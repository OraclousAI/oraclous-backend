"""DI providers (ORAA-4 §21 core layer) — wiring only.

The shared upstream HTTP client, the route table, and the proxy service are opened/built in
``core/lifespan`` and resolved per request from ``app.state``.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status
from oraclous_governance import Principal

from oraclous_application_gateway_service.core.auth import AuthError, verify_token
from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.domain.auth_policy import is_public
from oraclous_application_gateway_service.domain.upstreams import upstream_health_targets
from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
from oraclous_application_gateway_service.services.health_service import HealthService
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


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def get_edge_principal(request: Request) -> Principal | None:
    """Terminate identity at the edge: ``None`` for public allow-list paths, else a verified
    Principal (401 on missing/invalid/expired token — fail-closed before any upstream call)."""
    if is_public(request.url.path):
        return None
    token = _bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return verify_token(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_health_service(request: Request) -> HealthService:
    client = get_http_client(request)
    return HealthService(
        upstream_client=UpstreamClient(client),
        targets=upstream_health_targets(get_settings()),
    )


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
ProxyServiceDep = Annotated[ProxyService, Depends(get_proxy_service)]
EdgePrincipalDep = Annotated[Principal | None, Depends(get_edge_principal)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
