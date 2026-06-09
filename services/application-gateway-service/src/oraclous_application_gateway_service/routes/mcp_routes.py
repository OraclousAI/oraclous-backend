"""MCP server route (ORAA-4 §21 routes layer) — POST /v1/mcp, JSON-RPC over Streamable HTTP.

The integration-key bearer is the auth (a member JWT -> 403, in the dep). A single JSON-RPC message
per POST (the 2025-06-18 revision dropped batching); the three methods are request/response (no
server push), so a plain `application/json` response is conformant — no SSE stream is opened.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from oraclous_application_gateway_service.core.dependencies import McpKeyDep, McpServiceDep
from oraclous_application_gateway_service.domain.mcp_protocol import PARSE_ERROR, jsonrpc_error

router = APIRouter(tags=["mcp"])


@router.post("/v1/mcp")
async def mcp_endpoint(request: Request, key: McpKeyDep, service: McpServiceDep) -> Response:
    try:
        message = await request.json()
    except ValueError:
        return JSONResponse(jsonrpc_error(None, PARSE_ERROR, "parse error"))
    response = await service.dispatch(message, key)
    if response is None:  # a notification -> no body
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return JSONResponse(response)
