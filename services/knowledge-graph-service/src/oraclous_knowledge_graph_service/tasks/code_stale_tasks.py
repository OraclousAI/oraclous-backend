"""Stage 6 — code stale-symbol cleanup Celery task (#305).

A changed/deleted file marks its old symbols (and a deleted file's :File node) ``stale_at``
(Stage 1); this background sweep deletes those that have been stale longer than the TTL
(``KGS_CODE_STALE_TTL_DAYS``, default 7) — so a symbol removed from a file lingers (and stays
queryable) for a grace window, then is reaped. Two entry points:

  * ``cleanup_stale_code_task(graph_id, organisation_id)`` — sweep ONE graph; enqueued by the
    ingest task right after a code re-ingest that marked anything stale (decoupled from the ingest
    request), and the unit the integration tests drive. The sweep runs UNDER the SAME
    per-(org,graph) advisory lock the code-ingest critical section holds (#305): it SKIPS a
    graph that is mid-ingest
    (lock held by an ingest) so a DETACH DELETE never races a concurrent revive, and it DEDUPS
    overlapping sweeps (a second sweep finds the lock held and skips). Advisory — no Redis degrades
    to lock-off (the sweep still runs; it just loses the guard), exactly like the ingest path.
  * ``sweep_all_code_graphs_task()`` — the optional Celery-beat dispatcher: enumerate every
    (org, graph) pair that owns code ``:File`` nodes (via the repository's ``enumerate_code_graphs``
    — §21: the only Neo4j access stays in the repository layer, not here) and fan out one per-graph
    sweep each, BOUNDED by ``KGS_CODE_SWEEP_MAX_GRAPHS`` so a cadence never enumerates + fans out an
    unbounded set. Registered on ``celery_app.conf.beat_schedule`` so a beat process runs it on the
    configured cadence; harmless (never runs) when no beat is deployed.

Org context: a worker has no request, so the per-graph task carries ``organisation_id`` as a JSON
arg and binds it via ``use_organisation_context`` before any org-scoped substrate call (the
``ingest_tasks`` invariant). A task-scoped Neo4j driver is disposed per task (ADR-012).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.neo4j import make_neo4j_driver
from oraclous_knowledge_graph_service.core.redis import (
    RedisLock,
    RedisLockClient,
    make_redis_lock_client,
)
from oraclous_knowledge_graph_service.repositories.code_write_repository import (
    CodeGraphWriteRepository,
    enumerate_code_graphs,
)
from oraclous_knowledge_graph_service.services.code_ingestion_service import code_ingest_lock_key
from oraclous_knowledge_graph_service.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="kgs.cleanup_stale_code")
def cleanup_stale_code_task(graph_id: str, organisation_id: str) -> dict[str, Any]:
    """Sweep one graph's TTL-expired stale code symbols (org context bound from the arg).

    Held under the per-(org,graph) ingest lock: SKIP if a concurrent ingest (or another sweep) holds
    it — so the DETACH DELETE never races a revive and overlapping sweeps don't double-run."""
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
            key=code_ingest_lock_key(organisation_id=organisation_id, graph_id=graph_id),
            ttl_seconds=settings.code_ingest_lock_ttl_seconds,
        )
        token = lock.acquire()
        if token is None:
            # The lock is held — a re-ingest (must not be raced by a DETACH DELETE) or another
            # sweep (dedup) owns this graph. Skip; the next cadence / re-ingest re-enqueues.
            logger.info("stale-code sweep skipped: graph=%s is locked (mid-ingest/sweep)", graph_id)
            if lock_client is not None:
                _close(lock_client)
            return {"graph_id": graph_id, "deleted": 0, "skipped": "locked"}
        driver = make_neo4j_driver(settings)
        try:
            writer = CodeGraphWriteRepository(
                driver,
                graph_id=graph_id,
                organisation_id=organisation_id,
                database=settings.neo4j_database,
            )
            deleted = writer.delete_stale_symbols(ttl_days=settings.code_stale_ttl_days)
        finally:
            driver.close()
            lock.release(token)
            if lock_client is not None:
                _close(lock_client)
    logger.info("stale-code sweep: graph=%s deleted=%d", graph_id, deleted)
    return {"graph_id": graph_id, "deleted": deleted}


@celery_app.task(name="kgs.sweep_all_code_graphs")
def sweep_all_code_graphs_task() -> dict[str, Any]:
    """Beat dispatcher: fan out one per-graph sweep for every (org, graph) owning code files.

    Bounded by ``KGS_CODE_SWEEP_MAX_GRAPHS`` (0 = unbounded) so a cadence never enumerates + fans
    out an unbounded set; the enumeration is a repository read (§21)."""
    settings = get_settings()
    limit = settings.code_sweep_max_graphs or None
    driver = make_neo4j_driver(settings)
    try:
        pairs = enumerate_code_graphs(driver, database=settings.neo4j_database, limit=limit)
    finally:
        driver.close()
    for org, graph in pairs:
        cleanup_stale_code_task.delay(graph, org)
    return {"dispatched": len(pairs)}


def _close(lock_client: RedisLockClient) -> None:
    try:
        lock_client.close()
    except Exception as exc:  # noqa: BLE001 — best-effort close of the advisory lock client
        logger.debug("stale-code lock client close skipped: %s", exc)
