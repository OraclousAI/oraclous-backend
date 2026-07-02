"""MCP client connector (domain layer) — invoke a tool on an EXTERNAL MCP server.

The mirror of the gateway's MCP *server* (S8): an imported ``kind=tool, spec.type=mcp`` descriptor
points at a third-party MCP server; this executor calls its ``tools/call`` over the full
Streamable-HTTP protocol (#541: ``initialize`` → ``Mcp-Session-Id`` → ``notifications/initialized``
→ ``tools/call`` in-session, SSE responses parsed — see ``mcp_protocol``). Two security
controls wrap each call:

* **SSRF egress guard** — ``is_public_url`` (pure) PLUS an async DNS resolve that re-checks every
  resolved IP, so neither a literal internal IP nor a public hostname pointing inward is reached.
  #492: the guard RETURNS the vetted IP and the call CONNECTS to that pinned IP (Host + TLS SNI kept
  as the name), closing the DNS-rebinding TOCTOU.
* **Broker-held auth** — an optional ``api_key`` credential (resolved by the broker into the
  execution context, never stored here) is sent as a Bearer to the external server.

The raw MCP/transport error is NEVER surfaced to the caller (only a generic message + a coarse code)
matching the platform's no-leak rule for upstream errors.
"""

from __future__ import annotations

from typing import Any

import httpx

from oraclous_capability_registry_service.domain.connectors.mcp_protocol import (
    McpProtocolError,
    McpSession,
)
from oraclous_capability_registry_service.domain.egress import egress_allowed
from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

_TIMEOUT_S = 30.0


class McpToolExecutor(InternalTool):
    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        spec = self.descriptor.get("spec") or {}
        server_url = spec.get("server_url")
        tool_name = spec.get("tool_name")
        if not server_url or not tool_name:
            return ExecutionResult(
                success=False,
                error_message="an mcp tool descriptor needs spec.server_url + spec.tool_name",
                error_type="INVALID_SPEC",
            )
        pinned_ip = await egress_allowed(server_url)
        if pinned_ip is None:
            return ExecutionResult(
                success=False,
                error_message="the MCP server URL is not an allowed external target",
                error_type="EGRESS_BLOCKED",
            )
        creds = self.get_credentials(context, "api_key")
        bearer = creds["api_key"] if creds and creds.get("api_key") else None
        # #541: complete the Streamable-HTTP handshake (initialize → Mcp-Session-Id →
        # notifications/initialized) and issue tools/call WITHIN the session, parsing an SSE-framed
        # response — a real hosted MCP server needs all of this. #492: every request dials the
        # pinned vetted IP. follow_redirects=False: a 302 → internal URL would bypass the guard.
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_S, transport=self.transport, follow_redirects=False
            ) as client:
                session = McpSession(
                    client, server_url=server_url, pinned_ip=pinned_ip, bearer=bearer
                )
                await session.initialize()
                result = await session.call(
                    "tools/call", {"name": tool_name, "arguments": input_data}
                )
        except McpProtocolError as exc:
            meta = {"code": exc.rpc_code} if exc.rpc_code is not None else {}
            return ExecutionResult(
                success=False, error_message=str(exc), error_type=exc.code, metadata=meta
            )
        if result.get("isError"):
            return ExecutionResult(
                success=False,
                error_message="the MCP tool reported a failure",
                error_type="MCP_TOOL_ERROR",
            )
        return ExecutionResult(success=True, data={"content": result.get("content")})
