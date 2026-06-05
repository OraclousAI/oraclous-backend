"""Reverse-proxy catch-all route (ORAA-4 §21 routes layer).

A single catch-all that forwards any non-``/health`` request to its upstream and streams the
response straight back. Registered AFTER the health router so ``/health`` is served, never proxied.
Gateway domain errors map to 404 (unknown route) / 502 (upstream down) / 504 (upstream timeout) —
fail-closed, the edge never hangs.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from oraclous_application_gateway_service.core.dependencies import EdgePrincipalDep, ProxyServiceDep
from oraclous_application_gateway_service.domain.errors import (
    RouteNotFoundError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from oraclous_application_gateway_service.services.proxy_service import response_headers

router = APIRouter()

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


@router.api_route("/{path:path}", methods=_METHODS, response_model=None)
async def proxy(
    path: str, request: Request, svc: ProxyServiceDep, principal: EdgePrincipalDep
) -> StreamingResponse | JSONResponse:
    body = await request.body()
    try:
        upstream = await svc.open_upstream(
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            raw_headers=request.headers.raw,
            body=body,
            principal=principal,
        )
    except RouteNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error_code": "route_not_found",
                "detail": f"no upstream route for {request.url.path}",
            },
        )
    except UpstreamTimeoutError:
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={
                "error_code": "upstream_timeout",
                "detail": "the upstream did not respond in time",
            },
        )
    except UpstreamUnavailableError:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "error_code": "upstream_unavailable",
                "detail": "the upstream could not be reached",
            },
        )
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=dict(response_headers(upstream.headers.raw)),
        background=BackgroundTask(upstream.aclose),
    )
