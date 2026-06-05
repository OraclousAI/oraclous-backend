"""App lifecycle (ORAA-4 §21 core layer) — open/close the shared upstream HTTP client.

The gateway holds no database; its only external substrate is the upstream services, reached through
one shared ``httpx.AsyncClient`` (connection pooling + bounded timeouts) opened here and resolved
per request from ``app.state``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.domain.route_table import build_route_table
from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
from oraclous_application_gateway_service.services.proxy_service import ProxyService


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
        yield
    finally:
        await client.aclose()
