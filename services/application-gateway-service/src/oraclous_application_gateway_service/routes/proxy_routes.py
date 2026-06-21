"""Reverse-proxy catch-all route (routes layer).

A single catch-all that forwards any non-``/health`` request to its upstream. A successful (or
redirect) response is BUFFERED then returned whole; an upstream 4xx/5xx is NORMALISED into the
canonical error envelope under the same HTTP status — its body is drained and discarded,
never relayed, so an upstream stack trace / internal host / SQL error cannot leak through the edge
(Interface Contracts §3 rule 8). Buffering (not a detached stream) is deliberate: the generic proxy
relays bounded JSON API responses, and a ``StreamingResponse`` whose source connection was reclaimed
from the shared client's pool mid-iteration truncated large bodies into a terminator-less chunked
response (#235). Streaming/SSE surfaces (member chat) have their own router. Registered AFTER the
health router so ``/health`` is served, never proxied.
Gateway domain errors (RouteNotFound / UpstreamUnavailable / UpstreamTimeout) propagate to the
factory's exception handlers (404 / 502 / 504).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from oraclous_errors import ErrorCode, status_to_code
from starlette.responses import Response

from oraclous_application_gateway_service.core.dependencies import EdgePrincipalDep, ProxyServiceDep
from oraclous_application_gateway_service.domain.validation_passthrough import (
    extract_needs_credential,
    extract_validation_details,
)
from oraclous_application_gateway_service.schema.error import gateway_error
from oraclous_application_gateway_service.services.proxy_service import response_headers

router = APIRouter()

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


@router.api_route("/{path:path}", methods=_METHODS, response_model=None)
async def proxy(
    path: str, request: Request, svc: ProxyServiceDep, principal: EdgePrincipalDep
) -> Response:
    body = await request.body()
    # The server-minted correlation id (set on scope.state by RequestIdMiddleware at the edge) is
    # forwarded upstream so the request id survives end to end (WP-6). None → open_upstream forwards
    # no x-request-id (and still strips any client copy on the authenticated path via principal).
    request_id = getattr(request.state, "request_id", None)
    # RouteNotFound/UpstreamUnavailable/UpstreamTimeout propagate to the factory exception handlers.
    upstream = await svc.open_upstream(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        raw_headers=request.headers.raw,
        body=body,
        principal=principal,
        request_id=request_id,
    )
    if upstream.status_code >= 400:
        # Drain + close the upstream error body; it is never relayed (it may carry internals — §3
        # rule 8). aread() already closes on exhaustion; the explicit aclose() is the safety net.
        raw = await upstream.aread()
        await upstream.aclose()
        # 422 is the one case worth surfacing user-correctable signal: extract ONLY the field path +
        # the error-type machine token (never the value-reflecting msg) into VALIDATION_FAILED.
        if upstream.status_code == 422:
            details = extract_validation_details(raw)
            if details is not None:
                return gateway_error(
                    request,
                    code=ErrorCode.VALIDATION_FAILED,
                    status_code=422,
                    message="One or more fields failed validation.",
                    details=details,
                )
        # 409 with a credential miss: surface ONLY the leak-safe needs_credential token
        # ({requirement_id, provider}) so the caller knows which credential to onboard — selected
        # from the body, never from the status, so a genuine state-conflict 409 stays CONFLICT.
        if upstream.status_code == 409:
            needs_credential = extract_needs_credential(raw)
            if needs_credential is not None:
                return gateway_error(
                    request,
                    code=ErrorCode.CREDENTIALS_REQUIRED,
                    status_code=409,
                    needs_credential=needs_credential,
                )
        return gateway_error(
            request, code=status_to_code(upstream.status_code), status_code=upstream.status_code
        )
    # Success: buffer the whole body, then close — in ONE scope, so the source connection is never
    # reclaimed from the pool mid-iteration (the #235 truncation). ``response_headers`` strips the
    # upstream content-length; Starlette sets a correct one from the buffered body.
    try:
        upstream_body = await upstream.aread()
    finally:
        await upstream.aclose()
    return Response(
        content=upstream_body,
        status_code=upstream.status_code,
        headers=dict(response_headers(upstream.headers.raw)),
    )
