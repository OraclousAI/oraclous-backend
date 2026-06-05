"""Gateway own-error envelope (ORAA-4 §21 schema layer).

A forward-compatible SUBSET of the cross-service error envelope (ORA-56) for the gateway's OWN
errors only (401/404/502/504/503). Upstream errors are NOT enveloped — they pass through verbatim;
full cross-upstream normalization is pinned to R6. Carries a ``request_id`` (the incoming
``X-Request-Id`` or a fresh one) echoed in the ``X-Request-Id`` response header for correlation.
"""

from __future__ import annotations

import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class GatewayError(BaseModel):
    error_code: str
    message: str
    request_id: str


def request_id_of(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid.uuid4())


def gateway_error(
    request: Request, *, status_code: int, error_code: str, message: str
) -> JSONResponse:
    rid = request_id_of(request)
    body = GatewayError(error_code=error_code, message=message, request_id=rid)
    return JSONResponse(
        status_code=status_code, content=body.model_dump(), headers={"X-Request-Id": rid}
    )
