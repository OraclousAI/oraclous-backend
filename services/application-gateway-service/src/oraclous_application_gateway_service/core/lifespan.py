"""App lifecycle (ORAA-4 §21 core layer) — open/close the upstream HTTP client, Redis, and the
integration-key DB.

The gateway's substrate is the upstream services (one shared ``httpx.AsyncClient``), a Redis
connection used only by the edge rate limiter (Slice 2), and — since Slice 3 (ADR-019) — its own
Postgres holding the integration-key store. Both Redis and the DB are opened **graceful-degrade**:
if either cannot be created, its ``app.state`` slot is ``None`` so ``/health`` stays up; the rate
limiter then fails open, and the integration-key auth path returns 503 (never a crash).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI

from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.domain.route_table import build_route_table
from oraclous_application_gateway_service.repositories.chat_repository import ChatRepository
from oraclous_application_gateway_service.repositories.integration_key_repository import (
    IntegrationKeyRepository,
)
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)
from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
from oraclous_application_gateway_service.services.proxy_service import ProxyService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    timeout = httpx.Timeout(
        connect=settings.UPSTREAM_CONNECT_TIMEOUT,
        read=settings.UPSTREAM_READ_TIMEOUT,
        write=settings.UPSTREAM_READ_TIMEOUT,
        pool=settings.UPSTREAM_CONNECT_TIMEOUT,
    )
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    app.state.http_client = client
    app.state.route_table = build_route_table(settings)
    app.state.proxy_service = ProxyService(
        route_table=app.state.route_table,
        upstream_client=UpstreamClient(client),
        internal_key=settings.INTERNAL_SERVICE_KEY,
    )
    try:
        # short socket timeouts: from_url is lazy (connect-on-use), so a partitioned Redis must time
        # out fast on the first command, not block the edge for the OS default connect timeout.
        app.state.redis = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open: a Redis outage must not lock the edge
        logger.warning("gateway: Redis unavailable (%s); the edge rate limiter will fail open", exc)
        app.state.redis = None
    try:
        # graceful-degrade: a DB problem leaves /health up + the DB-backed routes return 503.
        app.state.integration_key_repo = IntegrationKeyRepository(settings.DATABASE_URL)
        app.state.published_agent_repo = PublishedAgentRepository(settings.DATABASE_URL)
        app.state.chat_repo = ChatRepository(settings.DATABASE_URL)
    except Exception as exc:  # noqa: BLE001 — never crash the edge on a DB issue
        logger.warning("gateway: datastore unavailable (%s); the DB-backed routes return 503", exc)
        app.state.integration_key_repo = None
        app.state.published_agent_repo = None
        app.state.chat_repo = None
    try:
        yield
    finally:
        await client.aclose()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        if app.state.integration_key_repo is not None:
            await app.state.integration_key_repo.close()
        if app.state.published_agent_repo is not None:
            await app.state.published_agent_repo.close()
        if app.state.chat_repo is not None:
            await app.state.chat_repo.close()
