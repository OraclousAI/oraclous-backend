"""Reverse-proxy catch-all route (ORAA-4 §21 routes layer).

A single catch-all that forwards any non-``/health`` request to its upstream and streams the
response straight back. Registered AFTER the health router so ``/health`` is served, never proxied.
Gateway domain errors (RouteNotFound / UpstreamUnavailable / UpstreamTimeout) propagate to the
factory's exception handlers, which emit the gateway own-error envelope (404 / 502 / 504).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from oraclous_application_gateway_service.core.dependencies import EdgePrincipalDep, ProxyServiceDep
from oraclous_application_gateway_service.services.proxy_service import response_headers

router = APIRouter()

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


@router.api_route("/{path:path}", methods=_METHODS, response_model=None)
async def proxy(
    path: str, request: Request, svc: ProxyServiceDep, principal: EdgePrincipalDep
) -> StreamingResponse:
    body = await request.body()
    # RouteNotFound/UpstreamUnavailable/UpstreamTimeout propagate to the factory exception handlers.
    upstream = await svc.open_upstream(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        raw_headers=request.headers.raw,
        body=body,
        principal=principal,
    )
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=dict(response_headers(upstream.headers.raw)),
        background=BackgroundTask(upstream.aclose),
    )
