"""App lifecycle (ORAA-4 §21 core layer) — open/close the read-only Neo4j driver (no schema)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from neo4j import Driver
from oraclous_telemetry import Severity, alert, evaluate_readiness, exit_on_degrade_enabled

from oraclous_knowledge_retriever_service.core.config import get_settings
from oraclous_knowledge_retriever_service.core.neo4j import make_neo4j_driver


def _open_neo4j() -> Driver | None:
    settings = get_settings()
    if not settings.neo4j_uri:
        return None
    # KRS is read-only (ORAA-58 / T6): it opens the driver but never creates schema/indexes.
    return make_neo4j_driver(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    driver: Driver | None = None
    # A configured Neo4j that fails to bind is a degradation; an UNSET URI is intentional (the
    # service serves /health but retrieval 503s), not a fault. Distinguish so /health is accurate.
    neo4j_bind_failed = False
    try:
        driver = await asyncio.to_thread(_open_neo4j)
    except Exception as exc:  # noqa: BLE001 — degrade: retrieval routes 503, /health reflects it
        neo4j_bind_failed = True
        alert(
            Severity.ERROR,
            "store_bind_failed",
            "knowledge-retriever-service",
            "Neo4j unavailable at startup; retrieval disabled",
            store="neo4j",
            error=str(exc),
        )
    app.state.neo4j_driver = driver
    app.state.neo4j_bind_failed = neo4j_bind_failed

    verdict = evaluate_readiness({"neo4j": None if neo4j_bind_failed else object()})
    if verdict.is_degraded and exit_on_degrade_enabled():
        raise SystemExit(1)

    try:
        yield
    finally:
        if driver is not None:
            await asyncio.to_thread(driver.close)
