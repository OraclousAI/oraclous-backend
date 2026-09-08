"""Chunk re-embed Celery tasks (#949 Q2) — the #303/#305 beat/sweep pattern.

When a workspace moves from the keyless hashing embedder to a real one, its stored chunks stay in
the old vector space. The read side refuses to compare across spaces rather than returning a
meaningless cosine, so a workspace in that state answers nothing until it is re-embedded. This does
that itself: there is no button the user has to find, and no window in which the platform quietly
serves scores computed against vectors from a different embedder.

The migration is cheap, which is the reason it can be automatic at all. The text does not change,
so the chunk ids do not change, so nothing that references a chunk — relationships, citations —
breaks. The expensive, id-rewriting migration is re-chunking, which is #948's problem, not this
one's.

Two entry points, mirroring `memory_tasks`:

  * ``reembed_chunks_task(graph_id, organisation_id)`` — one graph, under the per-(org,graph)
    advisory Redis lock, so two overlapping passes never fight over the same chunks. Advisory: no
    Redis degrades to lock-off rather than blocking the migration.
  * ``reembed_all_graphs_task()`` — the beat dispatcher, fanning out one job per graph still
    holding a stale chunk, bounded by ``KGS_REEMBED_SWEEP_MAX_GRAPHS``.

``run_reembed`` is deliberately lock-free and takes its repository and embedder as arguments, so it
can be driven against real substrate with no worker and no broker (see
``tests/integration/test_reembed_chunks_substrate.py``) — the same shape as ``run_consolidation``.

Org context: a worker has no request, so the per-graph task carries ``organisation_id`` as a JSON
arg and binds it before any org-scoped substrate call.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

from oraclous_embedding import embedder_identity
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.neo4j import make_neo4j_driver
from oraclous_knowledge_graph_service.core.redis import (
    RedisLock,
    RedisLockClient,
    make_redis_lock_client,
)
from oraclous_knowledge_graph_service.repositories.graph_generation_repository import (
    GraphGenerationRepository,
)
from oraclous_knowledge_graph_service.repositories.reembed_repository import (
    ReembedRepository,
    enumerate_graphs_needing_reembed,
)
from oraclous_knowledge_graph_service.services.credential_client import make_credential_broker
from oraclous_knowledge_graph_service.services.embedder import make_embedder
from oraclous_knowledge_graph_service.services.model_credential import (
    ModelCredentialUnavailable,
    credential_for_graph,
)
from oraclous_knowledge_graph_service.services.reembed_service import reembed_lock_key
from oraclous_knowledge_graph_service.tasks.celery_app import AsyncTaskExecutor, celery_app

logger = logging.getLogger(__name__)


class _StaleChunkStore(Protocol):
    """What ``run_reembed`` needs of a repository — the pass never touches a driver itself."""

    def list_chunks_needing_reembed(self, *, embedder_id: str, limit: int) -> list[dict]: ...

    def write_embedding(
        self, *, chunk_id: str, embedding: list[float], embedder_id: str, embedding_dim: int
    ) -> None: ...


class _Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def run_reembed(
    repo: _StaleChunkStore,
    embedder: _Embedder,
    *,
    embedder_id: str,
    embedding_dim: int,
    batch_size: int = 128,
) -> dict[str, Any]:
    """Re-embed one already-scoped workspace's stale chunks, in bounded batches.

    Restartable and idempotent by construction rather than by bookkeeping: the only thing that
    decides what to do next is "does this chunk already carry the target identity?", asked of the
    store. A crashed run resumes by asking again, a finished workspace answers with an empty page
    on the first ask, and a workspace part-way through is just one with fewer left. There is no
    cursor to lose and no resume state that can disagree with what was actually written.

    Each batch is re-read rather than paged through, because every write removes rows from the
    predicate — paging by offset over a shrinking set would skip chunks.
    """
    reembedded = 0
    while True:
        batch = repo.list_chunks_needing_reembed(embedder_id=embedder_id, limit=batch_size)
        if not batch:
            break
        vectors = embedder.embed([str(row["text"]) for row in batch])
        for row, vector in zip(batch, vectors, strict=True):
            repo.write_embedding(
                chunk_id=str(row["id"]),
                embedding=vector,
                embedder_id=embedder_id,
                embedding_dim=embedding_dim,
            )
            reembedded += 1
    return {"candidates": reembedded, "reembedded": reembedded}


@celery_app.task(name="kgs.reembed_chunks")
def reembed_chunks_task(graph_id: str, organisation_id: str) -> dict[str, Any]:
    """Re-embed one graph's stale chunks, under the advisory per-(org,graph) lock.

    A held lock means another pass is already working this graph: skip rather than queue, because
    the next sweep will find whatever is left. A skipped run must not open a Neo4j driver — the
    whole point of checking the lock first is to cost nothing when there is nothing to do.
    """
    settings = get_settings()
    context = OrganisationContext(
        organisation_id=uuid.UUID(organisation_id),
        principal_id=uuid.UUID(organisation_id),
        principal_type=PrincipalType.SERVICE_ACCOUNT,
    )
    with use_organisation_context(context):
        lock_client = make_redis_lock_client(settings)
        lock = RedisLock(
            lock_client,
            key=reembed_lock_key(organisation_id=organisation_id, graph_id=graph_id),
            ttl_seconds=settings.reembed_lock_ttl_seconds,
        )
        token = lock.acquire()
        if token is None:
            logger.info("chunk re-embed skipped: graph=%s is locked (mid-run)", graph_id)
            if lock_client is not None:
                _close(lock_client)
            return {"graph_id": graph_id, "reembedded": 0, "skipped": "locked"}
        # The organisation's own credential, resolved once for the whole pass (#724). The graph's
        # own pinned credential is deliberately not read here: the identity a chunk is stamped with
        # depends on the model and dimension, not on whose key paid for it, so the org default
        # produces vectors in exactly the target space.
        credential = AsyncTaskExecutor.run_async_task(
            credential_for_graph,
            settings,
            organisation_id=uuid.UUID(organisation_id),
            graph_id=uuid.UUID(graph_id),
            graph_credential_id=None,
            broker_factory=make_credential_broker,
        )
        driver = make_neo4j_driver(settings)
        try:
            try:
                embedder = make_embedder(settings, credential=credential)
            except ModelCredentialUnavailable:
                # This organisation has designated no model credential, so there is nothing to
                # re-embed WITH. Skip quietly rather than raise: the sweep visits every graph of
                # every organisation, and an unconfigured one would otherwise fail loudly once per
                # graph per cadence, forever. Nothing is lost — the workspace stays in its old
                # space and its searches refuse with a message that names the fix, and the next
                # sweep after they connect a model picks it up.
                logger.info(
                    "chunk re-embed skipped: graph=%s has no model credential to embed with",
                    graph_id,
                )
                return {"graph_id": graph_id, "reembedded": 0, "skipped": "no_model_credential"}
            repo = ReembedRepository(
                driver,
                organisation_id=organisation_id,
                graph_id=graph_id,
                database=settings.neo4j_database,
            )
            stats = run_reembed(
                repo,
                embedder,
                embedder_id=embedder_identity(settings),
                embedding_dim=settings.embedding_dim,
            )
            if stats["reembedded"]:
                # Bump the per-graph generation (#308), exactly as a completed ingest does. Every
                # chunk in this graph just moved to a different vector space, so a semantic or
                # hybrid response cached before the pass is now an answer computed in the OLD space
                # — and it would keep being served for the rest of the cache TTL. That is the same
                # stale-space output the whole re-embed exists to remove, arriving through the
                # cache instead of the store. Only on a pass that actually wrote something: a sweep
                # that found nothing stale has invalidated nothing.
                _bump_generation(
                    settings.redis_url, organisation_id=organisation_id, graph_id=graph_id
                )
        finally:
            driver.close()
            lock.release(token)
            if lock_client is not None:
                _close(lock_client)
    logger.info("chunk re-embed: graph=%s %s", graph_id, stats)
    return {"graph_id": graph_id, **stats}


@celery_app.task(name="kgs.reembed_all_graphs")
def reembed_all_graphs_task() -> dict[str, Any]:
    """Beat dispatcher: one per-graph job for every (org, graph) still holding a stale chunk.

    This is what makes the migration automatic. A workspace nobody happens to search still gets
    re-embedded, so its first search after the change works rather than being the thing that
    discovers the problem.
    """
    settings = get_settings()
    limit = settings.reembed_sweep_max_graphs or None
    driver = make_neo4j_driver(settings)
    try:
        pairs = enumerate_graphs_needing_reembed(
            driver,
            embedder_id=embedder_identity(settings),
            database=settings.neo4j_database,
            limit=limit,
        )
    finally:
        driver.close()
    for org, graph in pairs:
        reembed_chunks_task.delay(graph, org)
    return {"dispatched": len(pairs)}


def _bump_generation(redis_url: str, *, organisation_id: str, graph_id: str) -> None:
    """Signal the retriever that this graph's cached reads are stale (#308) — advisory.

    The bump itself already swallows Redis errors, but opening the short-lived client can fail on
    its own (an unreachable or malformed URL). A re-embed pass that has already written its vectors
    must not be reported as failed over an invalidation hint: the cache falls back to TTL expiry,
    which is a bounded delay, whereas a raised error here would re-run a pass that is idempotent
    but not free.
    """
    try:
        GraphGenerationRepository.bump_for(
            redis_url=redis_url, organisation_id=organisation_id, graph_id=graph_id
        )
    except Exception as exc:  # noqa: BLE001 — advisory: a failed bump falls back to TTL expiry
        logger.warning(
            "chunk re-embed: generation bump skipped (graph=%s): %s — retriever cache will "
            "TTL-expire instead",
            graph_id,
            exc,
        )


def _close(lock_client: RedisLockClient) -> None:
    try:
        lock_client.close()
    except Exception as exc:  # noqa: BLE001 — best-effort close of the advisory lock client
        logger.debug("chunk re-embed lock client close skipped: %s", exc)
