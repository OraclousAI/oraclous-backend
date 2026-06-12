"""Memory consolidation Celery tasks (#332 / ADR-027 §3 — the #305 beat/sweep pattern).

True similarity-based consolidation: per (org, graph), cluster the CURRENT memories whose stored
embeddings sit above the cosine threshold (``domain/memory_consolidation`` — pure, controlled-
vector-testable) and merge each cluster — the highest-importance memory wins, absorbing the losers'
importance (capped 1.0); losers are invalidated + SUPERSEDES-linked. Two entry points:

  * ``consolidate_memories_task(graph_id, organisation_id)`` — consolidate ONE graph; enqueued by
    ``POST /api/v1/graphs/{id}/memories/consolidate``. Runs UNDER the per-(org,graph) advisory
    Redis lock (``core/redis.RedisLock``, #303/#305): a held lock (another consolidation mid-run)
    SKIPS — overlapping passes never double-merge. Advisory: no Redis degrades to lock-off.
  * ``consolidate_all_memory_graphs_task()`` — the optional Celery-beat dispatcher: enumerate every
    (org, graph) owning current :Memory nodes (a repository read — §21) and fan out one per-graph
    job each, BOUNDED by ``KGS_MEMORY_SWEEP_MAX_GRAPHS``. Harmless (never runs) without a beat.

Org context: a worker has no request, so the per-graph task carries ``organisation_id`` as a JSON
arg and binds it via ``use_organisation_context`` before any org-scoped substrate call (the
``ingest_tasks`` invariant). A task-scoped Neo4j driver is disposed per task (ADR-012).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.neo4j import make_neo4j_driver
from oraclous_knowledge_graph_service.core.redis import RedisLock, make_redis_lock_client
from oraclous_knowledge_graph_service.domain.memory_consolidation import (
    MemoryVector,
    cluster_by_similarity,
)
from oraclous_knowledge_graph_service.repositories.memory_repository import (
    MemoryRepository,
    enumerate_memory_graphs,
)
from oraclous_knowledge_graph_service.services.memory_service import (
    memory_consolidation_lock_key,
)
from oraclous_knowledge_graph_service.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def run_consolidation(
    repo: MemoryRepository,
    *,
    threshold: float,
    max_memories: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One consolidation pass over an already-scoped repository (the lock-free core, so the
    integration tests drive it against the real substrate without a worker/broker)."""
    if now is None:
        now = datetime.now(UTC)
    rows = repo.list_current_with_embeddings(limit=max_memories)
    items = [
        MemoryVector(
            memory_id=str(r["memory_id"]),
            embedding=tuple(float(x) for x in (r["embedding"] or ())),
            importance=float(r["base_importance"] or 0.0),
        )
        for r in rows
    ]
    clusters = cluster_by_similarity(items, threshold=threshold)
    merged = 0
    for cluster in clusters:
        merged += repo.merge_memories(
            winner_id=cluster.winner_id, loser_ids=list(cluster.loser_ids), now=now
        )
    return {"candidates": len(items), "clusters": len(clusters), "merged": merged}


@celery_app.task(name="kgs.consolidate_memories")
def consolidate_memories_task(graph_id: str, organisation_id: str) -> dict[str, Any]:
    """Consolidate one graph's memories (org context bound from the arg), under the advisory
    per-(org,graph) lock — a held lock (another pass mid-run) skips, never double-merges."""
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
            key=memory_consolidation_lock_key(organisation_id=organisation_id, graph_id=graph_id),
            ttl_seconds=settings.memory_consolidation_lock_ttl_seconds,
        )
        token = lock.acquire()
        if token is None:
            logger.info("memory consolidation skipped: graph=%s is locked (mid-run)", graph_id)
            if lock_client is not None:
                _close(lock_client)
            return {"graph_id": graph_id, "merged": 0, "skipped": "locked"}
        driver = make_neo4j_driver(settings)
        try:
            repo = MemoryRepository(
                driver,
                graph_id=graph_id,
                organisation_id=organisation_id,
                database=settings.neo4j_database,
            )
            stats = run_consolidation(
                repo,
                threshold=settings.memory_consolidation_similarity_threshold,
                max_memories=settings.memory_consolidation_max_memories,
            )
        finally:
            driver.close()
            lock.release(token)
            if lock_client is not None:
                _close(lock_client)
    logger.info("memory consolidation: graph=%s %s", graph_id, stats)
    return {"graph_id": graph_id, **stats}


@celery_app.task(name="kgs.consolidate_all_memory_graphs")
def consolidate_all_memory_graphs_task() -> dict[str, Any]:
    """Beat dispatcher: fan out one per-graph consolidation for every (org, graph) owning current
    memories, bounded by ``KGS_MEMORY_SWEEP_MAX_GRAPHS`` (0 = unbounded)."""
    settings = get_settings()
    limit = settings.memory_sweep_max_graphs or None
    driver = make_neo4j_driver(settings)
    try:
        pairs = enumerate_memory_graphs(driver, database=settings.neo4j_database, limit=limit)
    finally:
        driver.close()
    for org, graph in pairs:
        consolidate_memories_task.delay(graph, org)
    return {"dispatched": len(pairs)}


def _close(lock_client: object) -> None:
    try:
        lock_client.close()
    except Exception as exc:  # noqa: BLE001 — best-effort close of the advisory lock client
        logger.debug("memory consolidation lock client close skipped: %s", exc)
