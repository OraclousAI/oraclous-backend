"""App lifecycle (ORAA-4 §21 core layer) — open/close shared connections.

The Postgres engine + sessionmaker and (when `KGS_NEO4J_URI` is set) the Neo4j driver are built once
at startup and disposed at shutdown, exposed on `app.state`. The Neo4j schema (org-scoped indexes)
is applied idempotently at startup. If Neo4j is unconfigured or unreachable, the API still serves
graph CRUD + /health; ingestion endpoints then return 503 (the driver is None). Connection setup is
the one driver concern allowed outside `repositories/` (§21 rule 3).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from neo4j import Driver
from oraclous_telemetry import Severity, alert, evaluate_readiness, exit_on_degrade_enabled

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.database import make_engine, make_sessionmaker
from oraclous_knowledge_graph_service.core.neo4j import ensure_schema, make_neo4j_driver


def _open_neo4j() -> Driver | None:
    settings = get_settings()
    if not settings.neo4j_uri:
        return None
    driver = make_neo4j_driver(settings)
    ensure_schema(driver, database=settings.neo4j_database)
    return driver


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = make_engine()
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    driver: Driver | None = None
    # A configured Neo4j that fails to bind is a degradation; an UNSET URI is intentional CRUD-only
    # operation, not a fault. Distinguish the two so /health flags degraded only on a real failure.
    neo4j_bind_failed = False
    try:
        driver = await asyncio.to_thread(_open_neo4j)
    except Exception as exc:  # noqa: BLE001 — degrade to CRUD-only, do not crash the app
        neo4j_bind_failed = True
        alert(
            Severity.ERROR,
            "store_bind_failed",
            "knowledge-graph-service",
            "Neo4j unavailable at startup; ingestion disabled",
            store="neo4j",
            error=str(exc),
        )
    app.state.neo4j_driver = driver
    app.state.neo4j_bind_failed = neo4j_bind_failed

    # Readiness reflects the critical store (Neo4j). A configured-but-failed bind is degraded; an
    # unset URI is not. Flag-gated crash-on-degrade (default OFF) lets a managed deploy restart.
    verdict = evaluate_readiness({"neo4j": None if neo4j_bind_failed else object()})
    if verdict.is_degraded and exit_on_degrade_enabled():
        raise SystemExit(1)

    try:
        yield
    finally:
        if driver is not None:
            await asyncio.to_thread(driver.close)
        await engine.dispose()
