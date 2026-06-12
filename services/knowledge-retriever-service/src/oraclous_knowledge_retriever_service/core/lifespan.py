"""App lifecycle (ORAA-4 §21 core layer) — open/close the read-only Neo4j driver (no schema).

Also opens an advisory async Redis client for the query cache (#308) when KRS_QUERY_CACHE is on;
a Redis that fails to bind degrades to cache-off (the read path still serves), never a hard stop —
the cache is advisory, only Neo4j gates readiness.
"""

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


def _open_redis():
    """Open an async Redis client for the query cache, or None when disabled (#308)."""
    settings = get_settings()
    if not settings.query_cache:
        return None
    from redis import asyncio as aioredis

    return aioredis.from_url(settings.redis_url, decode_responses=True)


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

    # Advisory query cache (#308): a configured Redis that fails to bind degrades to cache-off — it
    # never gates readiness (only Neo4j does), so a Redis outage cannot take retrieval down.
    redis_client = None
    try:
        redis_client = _open_redis()
    except Exception as exc:  # noqa: BLE001 — cache is advisory: degrade to cache-off, serve reads
        alert(
            Severity.WARNING,
            "query_cache_bind_failed",
            "knowledge-retriever-service",
            "Redis unavailable at startup; query cache disabled (reads still served live)",
            store="redis",
            error=str(exc),
        )
    app.state.redis_client = redis_client

    verdict = evaluate_readiness({"neo4j": None if neo4j_bind_failed else object()})
    if verdict.is_degraded and exit_on_degrade_enabled():
        raise SystemExit(1)

    try:
        yield
    finally:
        if driver is not None:
            await asyncio.to_thread(driver.close)
        if redis_client is not None:
            await redis_client.aclose()
