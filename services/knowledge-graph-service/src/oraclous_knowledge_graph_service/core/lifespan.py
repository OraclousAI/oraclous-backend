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
from oraclous_knowledge_graph_service.core.neo4j import (
    ensure_schema,
    make_neo4j_async_driver,
    make_neo4j_driver,
)
from oraclous_knowledge_graph_service.core.redis import make_redis_lock_client
from oraclous_knowledge_graph_service.core.rls import (
    RlsBypassingRoleError,
    assert_runtime_role_isolates,
)


def _open_neo4j() -> Driver | None:
    settings = get_settings()
    if not settings.neo4j_uri:
        return None
    driver = make_neo4j_driver(settings)
    ensure_schema(driver, database=settings.neo4j_database)
    return driver


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = make_engine()
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    # ADR-030 §3: fail closed LOUDLY if the runtime role bypasses RLS (a superuser / BYPASSRLS role
    # makes the FORCE'd policy inert — T1-M3). A mis-deployed bypassing role is a hard configuration
    # error, so it exits the process rather than quietly serving an unscoped store. Gated on
    # KGS_RLS_ASSERT_RUNTIME_ROLE (the deployed oraclous_app web + worker set it; a deliberate
    # owner-DSN dev/test run leaves it off). The engine the request path uses is the one asserted.
    if settings.rls_assert_runtime_role:
        try:
            await assert_runtime_role_isolates(engine)
        except RlsBypassingRoleError as exc:
            alert(
                Severity.ERROR,
                "rls_runtime_role_bypasses",
                "knowledge-graph-service",
                "runtime DB role bypasses RLS; refusing to start (ADR-030 §3)",
                error=str(exc),
            )
            await engine.dispose()
            raise SystemExit(1) from exc

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

    # An async Neo4j driver JUST for the ReBAC engine (cross-org grant writes). Best-effort: a bind
    # failure disables granting but never gates readiness or graph CRUD (which use the sync driver).
    app.state.neo4j_async_driver = None
    if not neo4j_bind_failed:
        try:
            app.state.neo4j_async_driver = await make_neo4j_async_driver(get_settings())
        except Exception as exc:  # noqa: BLE001 — degrade to grant-off, do not crash the app
            alert(
                Severity.ERROR,
                "rebac_store_bind_failed",
                "knowledge-graph-service",
                "Neo4j async driver unavailable; cross-org grants disabled",
                store="neo4j",
                error=str(exc),
            )

    # Advisory sync Redis client for the per-(org,graph) community-detect lock (#303). A Redis
    # outage degrades to lock-off (detection still runs) — it never gates readiness.
    app.state.detect_lock_client = await asyncio.to_thread(make_redis_lock_client, get_settings())

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
        async_driver = getattr(app.state, "neo4j_async_driver", None)
        if async_driver is not None:
            await async_driver.close()
        lock_client = getattr(app.state, "detect_lock_client", None)
        if lock_client is not None:
            await asyncio.to_thread(lock_client.close)
        await engine.dispose()
