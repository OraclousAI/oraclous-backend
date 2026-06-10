"""MCP client connector (ORAA-4 §21 domain layer) — invoke a tool on an EXTERNAL MCP server.

The mirror of the gateway's MCP *server* (S8): an imported ``kind=tool, spec.type=mcp`` descriptor
points at a third-party MCP server; this executor calls its ``tools/call`` over the same hand-built
JSON-RPC-2.0-over-HTTP subset. Two security controls wrap each call:

* **SSRF egress guard** — ``is_public_url`` (pure) PLUS an async DNS resolve that re-checks every
  resolved IP, so neither a literal internal IP nor a public hostname pointing inward is reached.
* **Broker-held auth** — an optional ``api_key`` credential (resolved by the broker into the
  execution context, never stored here) is sent as a Bearer to the external server.

The raw MCP/transport error is NEVER surfaced to the caller (only a generic message + a coarse code)
matching the platform's no-leak rule for upstream errors.
"""

from __future__ import annotations

from typing import Any

import httpx

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
        if not await egress_allowed(server_url):
            return ExecutionResult(
                success=False,
                error_message="the MCP server URL is not an allowed external target",
                error_type="EGRESS_BLOCKED",
            )
        headers = {"content-type": "application/json", "accept": "application/json"}
        creds = self.get_credentials(context, "api_key")
        if creds and creds.get("api_key"):
            headers["authorization"] = f"Bearer {creds['api_key']}"
        rpc = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": input_data,  # InternalTool already validated this is a JSON object
            },
        }
        try:
            # follow_redirects=False (httpx's default, made explicit): a 302 → an internal URL would
            # bypass the egress guard, so a redirect is treated as a non-200 error, never followed.
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_S, transport=self.transport, follow_redirects=False
            ) as client:
                resp = await client.post(server_url, json=rpc, headers=headers)
        except httpx.HTTPError:
            return ExecutionResult(
                success=False,
                error_message="the MCP server could not be reached",
                error_type="MCP_UNREACHABLE",
            )
        return self._result_from_response(resp)

    @staticmethod
    def _result_from_response(resp: httpx.Response) -> ExecutionResult:
        if resp.status_code != 200:
            return ExecutionResult(
                success=False,
                error_message=f"the MCP server returned {resp.status_code}",
                error_type="MCP_HTTP_ERROR",
                metadata={"status_code": resp.status_code},
            )
        try:
            body = resp.json()
        except ValueError:
            return ExecutionResult(
                success=False,
                error_message="the MCP server returned a non-JSON body",
                error_type="MCP_BAD_RESPONSE",
            )
        # a hostile server can return ANY JSON value (a list, a bare string) — type-guard before any
        # ``.get`` so a malformed body is a clean MCP_BAD_RESPONSE, never an AttributeError that
        # would leak the internal exception (the no-leak contract).
        if not isinstance(body, dict):
            return ExecutionResult(
                success=False,
                error_message="the MCP server returned a malformed JSON-RPC body",
                error_type="MCP_BAD_RESPONSE",
            )
        if body.get("error"):
            # a JSON-RPC error object — surface only the coarse code, never the raw message
            code = (
                (body.get("error") or {}).get("code")
                if isinstance(body.get("error"), dict)
                else None
            )
            return ExecutionResult(
                success=False,
                error_message="the MCP tool returned an error",
                error_type="MCP_TOOL_ERROR",
                metadata={"code": code},
            )
        result = body.get("result")
        if not isinstance(result, dict):
            return ExecutionResult(
                success=False,
                error_message="the MCP server returned a malformed JSON-RPC result",
                error_type="MCP_BAD_RESPONSE",
            )
        if result.get("isError"):
            return ExecutionResult(
                success=False,
                error_message="the MCP tool reported a failure",
                error_type="MCP_TOOL_ERROR",
            )
        return ExecutionResult(success=True, data={"content": result.get("content")})
