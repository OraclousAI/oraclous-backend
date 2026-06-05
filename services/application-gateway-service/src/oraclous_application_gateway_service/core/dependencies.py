"""DI providers (ORAA-4 §21 core layer) — wiring only.

The shared upstream HTTP client, the route table, and the proxy service are opened/built in
``core/lifespan`` and resolved per request from ``app.state``.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status

from oraclous_application_gateway_service.services.proxy_service import ProxyService


def get_http_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="gateway HTTP client unavailable",
        )
    return client


def get_proxy_service(request: Request) -> ProxyService:
    svc = getattr(request.app.state, "proxy_service", None)
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="gateway proxy unavailable",
        )
    return svc


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
ProxyServiceDep = Annotated[ProxyService, Depends(get_proxy_service)]
