"""Stage 6 — code stale-symbol cleanup Celery task (#305).

A changed file marks its old symbols ``stale_at`` (Stage 1); this background sweep deletes those
that have been stale longer than the TTL (``KGS_CODE_STALE_TTL_DAYS``, default 7) — so a symbol
removed from a file lingers (and stays queryable) for a grace window, then is reaped. Two entry
points:

  * ``cleanup_stale_code_task(graph_id, organisation_id)`` — sweep ONE graph; enqueued by the
    ingest task right after a code re-ingest that marked anything stale (decoupled from the ingest
    request), and the unit that the cross-org isolation test drives.
  * ``sweep_all_code_graphs_task()`` — the optional Celery-beat dispatcher: enumerate every
    (org, graph) pair that owns code ``:File`` nodes (read straight off Neo4j, which carries org +
    graph on every node — no per-org Postgres context needed) and fan out one per-graph sweep each.
    Registered on ``celery_app.conf.beat_schedule`` so a beat process runs it on the configured
    cadence; harmless (never runs) when no beat is deployed.

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
from oraclous_knowledge_graph_service.repositories.code_write_repository import (
    CodeGraphWriteRepository,
)
from oraclous_knowledge_graph_service.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="kgs.cleanup_stale_code")
def cleanup_stale_code_task(graph_id: str, organisation_id: str) -> dict[str, Any]:
    """Sweep one graph's TTL-expired stale code symbols (org context bound from the arg)."""
    settings = get_settings()
    context = OrganisationContext(
        organisation_id=uuid.UUID(organisation_id),
        principal_id=uuid.UUID(organisation_id),
        principal_type=PrincipalType.SERVICE_ACCOUNT,
    )
    with use_organisation_context(context):
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
    logger.info("stale-code sweep: graph=%s deleted=%d", graph_id, deleted)
    return {"graph_id": graph_id, "deleted": deleted}


@celery_app.task(name="kgs.sweep_all_code_graphs")
def sweep_all_code_graphs_task() -> dict[str, Any]:
    """Beat dispatcher: fan out one per-graph sweep for every (org, graph) owning code files."""
    settings = get_settings()
    driver = make_neo4j_driver(settings)
    try:
        result = driver.execute_query(
            """
            MATCH (f:File {ingestion_source: 'code'})
            WHERE f.organisation_id IS NOT NULL AND f.graph_id IS NOT NULL
            RETURN DISTINCT f.organisation_id AS org, f.graph_id AS graph
            """,
            database_=settings.neo4j_database,
        )
        pairs = [(rec["org"], rec["graph"]) for rec in result.records]
    finally:
        driver.close()
    for org, graph in pairs:
        cleanup_stale_code_task.delay(graph, org)
    return {"dispatched": len(pairs)}
