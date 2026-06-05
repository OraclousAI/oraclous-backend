"""Gateway own-error envelope (ORAA-4 §21 schema layer).

Builds the canonical ORA-37 error envelope (``{"error": {...}}``) via the shared
``oraclous_errors`` emitter for the gateway's OWN errors. The server-minted
``requestId`` is read from ``request.state`` (set by ``RequestIdMiddleware``) and
echoed in the ``X-Request-Id`` response header by that middleware. Messages are the
curated, generic policy messages — never the exception detail or the request path
(Interface Contracts §3).
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from oraclous_errors import ErrorCode, build_envelope, http_status_for, new_request_id


def request_id_of(request: Request) -> str:
    """The server-minted correlation id for this request.

    ``RequestIdMiddleware`` sets it on ``request.state``; a fresh id is minted
    defensively if the middleware did not run (e.g. a direct unit call).
    """
    rid = getattr(request.state, "request_id", None)
    return rid if isinstance(rid, str) else new_request_id()


def gateway_error(
    request: Request,
    *,
    code: ErrorCode,
    status_code: int | None = None,
    message: str | None = None,
    retryable: bool | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a contract-conformant error response for one of the gateway's own errors.

    ``status_code`` defaults to the code's taxonomy status; pass it explicitly to
    preserve a more specific HTTP semantic (e.g. 502 Bad Gateway with a
    SERVICE_UNAVAILABLE code). ``headers`` (e.g. ``WWW-Authenticate``) are attached
    verbatim; ``X-Request-Id`` is added by the middleware.
    """
    body = build_envelope(
        code, request_id=request_id_of(request), message=message, retryable=retryable
    )
    return JSONResponse(
        status_code=status_code if status_code is not None else http_status_for(code),
        content=body,
        headers=headers,
    )
