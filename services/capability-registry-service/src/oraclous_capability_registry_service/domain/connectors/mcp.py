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


def _arguments(input_data: dict[str, Any]) -> dict[str, Any]:
    """The MCP ``arguments`` for a dispatch, with the registry's internal routing key removed.

    #698 D3: the harness loop dispatches every tool call as ``{"operation": <op>, **args}`` — that
    key selects the descriptor's operation and means nothing to a third-party server. Forwarded
    verbatim it becomes an unexpected property, which a schema-validating server rejects outright.
    The strip lives here rather than in the loop so every caller of the dispatch contract inherits
    it, and it is deliberately SHALLOW: a nested ``operation`` is the tool's own argument.
    """
    return {k: v for k, v in input_data.items() if k != "operation"}


_TOOL_ERROR_CHARS = 300  # the run page's per-step budget; a tool may answer with a wall of text


def _tool_error_message(content: Any) -> str:
    """WHY the MCP tool refused, in the tool's own words, bounded (#697's live blocker).

    ``the MCP tool reported a failure`` is equally true of a wrong argument, a token without write
    access and a server that is down — so it told an operator nothing. On run 43174243 a member
    posted a finished review through ``add_issue_comment``, got that sentence back, and neither the
    run page nor the member could say what to change.

    A tool result's ``content`` is the tool's ANSWER to its caller, not an internal detail, which
    is why it is surfaced where the JSON-RPC transport error is still withheld (that one can carry
    the server's own internals). Text blocks only, joined and capped; a non-text block (an image, a
    resource handle) is not a reason and is skipped.
    """
    base = "the MCP tool reported a failure"
    if not isinstance(content, list):
        return base
    said = " ".join(
        block["text"].strip()
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    )
    if not said:
        return base
    return f"{base}: {said[:_TOOL_ERROR_CHARS]}"


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
                    "tools/call", {"name": tool_name, "arguments": _arguments(input_data)}
                )
        except McpProtocolError as exc:
            meta = {"code": exc.rpc_code} if exc.rpc_code is not None else {}
            return ExecutionResult(
                success=False, error_message=str(exc), error_type=exc.code, metadata=meta
            )
        if result.get("isError"):
            return ExecutionResult(
                success=False,
                error_message=_tool_error_message(result.get("content")),
                error_type="MCP_TOOL_ERROR",
            )
        return ExecutionResult(success=True, data={"content": result.get("content")})
