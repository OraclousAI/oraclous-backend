"""App lifecycle (ORAA-4 §21 core layer) — open/close the Postgres engine + Redis.

Wires the async sessionmaker (used by the user-identity routes) and a Redis client (used by the
rate limiter) into `app.state`. Degrades gracefully: if Postgres/Redis are unreachable at startup
the app still serves `/health`, and the identity routes report 503 until the store is configured.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from oraclous_telemetry import Severity, alert, evaluate_readiness, exit_on_degrade_enabled

from oraclous_auth_service.core.config import get_settings
from oraclous_auth_service.core.database import make_engine, make_sessionmaker

_SERVICE = "auth-service"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = None
    try:
        engine = make_engine()
        app.state.sessionmaker = make_sessionmaker(engine)
    except Exception as exc:  # noqa: BLE001 — degrade: identity routes 503, /health reflects it
        app.state.sessionmaker = None
        alert(
            Severity.ERROR,
            "store_bind_failed",
            _SERVICE,
            "Postgres unavailable at startup; identity routes disabled",
            store="postgres",
            error=str(exc),
        )
    try:
        app.state.redis = aioredis.from_url(settings.redis_url)
    except Exception as exc:  # noqa: BLE001 — rate limiter fails open without Redis (non-critical)
        app.state.redis = None
        alert(
            Severity.WARNING,
            "store_bind_failed",
            _SERVICE,
            "Redis unavailable at startup; rate limiting fails open",
            store="redis",
            error=str(exc),
        )

    # Readiness reflects the CRITICAL store (Postgres). Redis is fail-open and does not flip it.
    # Flag-gated crash-on-degrade (default OFF) lets a managed deploy ask the orchestrator to
    # restart a service that came up without its store; dev/CI never crash-loop.
    verdict = evaluate_readiness({"postgres": app.state.sessionmaker})
    if verdict.is_degraded and exit_on_degrade_enabled():
        raise SystemExit(1)

    try:
        yield
    finally:
        if app.state.redis is not None:
            await app.state.redis.aclose()
        if engine is not None:
            await engine.dispose()
