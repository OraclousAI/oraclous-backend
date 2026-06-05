"""Reverse-proxy catch-all route (ORAA-4 §21 routes layer).

A single catch-all that forwards any non-``/health`` request to its upstream. A successful (or
redirect) response is streamed straight back; an upstream 4xx/5xx is NORMALISED into the canonical
ORA-37 error envelope under the same HTTP status — its body is drained and discarded, never relayed,
so an upstream stack trace / internal host / SQL error cannot leak through the edge (Interface
Contracts §3 rule 8). Registered AFTER the health router so ``/health`` is served, never proxied.
Gateway domain errors (RouteNotFound / UpstreamUnavailable / UpstreamTimeout) propagate to the
factory's exception handlers (404 / 502 / 504).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from oraclous_errors import status_to_code
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from oraclous_application_gateway_service.core.dependencies import EdgePrincipalDep, ProxyServiceDep
from oraclous_application_gateway_service.schema.error import gateway_error
from oraclous_application_gateway_service.services.proxy_service import response_headers

router = APIRouter()

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


@router.api_route("/{path:path}", methods=_METHODS, response_model=None)
async def proxy(
    path: str, request: Request, svc: ProxyServiceDep, principal: EdgePrincipalDep
) -> Response:
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
    if upstream.status_code >= 400:
        # Drain + close the upstream error body and re-emit the canonical envelope under the same
        # status; the upstream's body is never relayed (it may carry internals — §3 rule 8).
        await upstream.aread()
        await upstream.aclose()
        return gateway_error(
            request, code=status_to_code(upstream.status_code), status_code=upstream.status_code
        )
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=dict(response_headers(upstream.headers.raw)),
        background=BackgroundTask(upstream.aclose),
    )
