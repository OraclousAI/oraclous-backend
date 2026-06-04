"""App lifecycle (ORAA-4 §21 core layer) — open/close the Postgres engine + Redis.

Wires the async sessionmaker (used by the user-identity routes) and a Redis client (used by the
rate limiter) into `app.state`. Degrades gracefully: if Postgres/Redis are unreachable at startup
the app still serves `/health`, and the identity routes report 503 until the store is configured.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from oraclous_auth_service.core.config import get_settings
from oraclous_auth_service.core.database import make_engine, make_sessionmaker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = None
    try:
        engine = make_engine()
        app.state.sessionmaker = make_sessionmaker(engine)
    except Exception as exc:  # noqa: BLE001 — degrade: identity routes 503, /health still serves
        logger.warning("Postgres unavailable at startup; identity routes disabled: %s", exc)
        app.state.sessionmaker = None
    try:
        app.state.redis = aioredis.from_url(settings.redis_url)
    except Exception as exc:  # noqa: BLE001 — rate limiter fails open without Redis
        logger.warning("Redis unavailable at startup; rate limiting fails open: %s", exc)
        app.state.redis = None
    try:
        yield
    finally:
        if app.state.redis is not None:
            await app.state.redis.aclose()
        if engine is not None:
            await engine.dispose()
