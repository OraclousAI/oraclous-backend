"""MCP / JSON-RPC protocol helpers (domain layer) — pure, no I/O.

A hand-rolled subset of the Model Context Protocol over JSON-RPC 2.0: ``initialize`` + `tools/list`
+ ``tools/call`` (no resources/prompts/sampling in S8). The gateway exposes the org's PUBLISHED
AGENTS as MCP tools (one tool per agent, a fixed single-string `input` schema mirroring the invoke
surface), and a tools/call routes through the existing synchronous published-agent invoke path. The
adapter (services/mcp_service) supplies the I/O; everything here is a pure projection.
"""

from __future__ import annotations

from typing import Any

PROTOCOL_VERSION = "2025-06-18"  # the MCP revision this subset targets
SERVER_INFO = {"name": "oraclous-application-gateway", "version": "1"}

# JSON-RPC 2.0 error codes (+ the MCP-relevant ones)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def initialize_result() -> dict[str, Any]:
    """The MCP ``initialize`` handshake — advertise the tools capability only."""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
    }


def tool_descriptor(
    *, slug: str, display_name: str | None, description: str | None
) -> dict[str, Any]:
    """One published agent -> one MCP tool with a fixed single-string `input` schema (typed —
    no invented per-agent arg schemas; this mirrors the invoke surface)."""
    return {
        "name": slug,
        "description": description or display_name or f"The {slug} agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "The goal/prompt for the agent."}
            },
            "required": ["input"],
        },
    }


def call_result_from_invoke(
    *, status: str, output: str | None, execution_id: str
) -> dict[str, Any]:
    """Project the coarse public invoke status onto an MCP tools/call result. ``succeeded`` -> the
    output as text; ``failed`` -> isError (generic — the raw error never leaks); ``pending``
    (a human/HITL step) -> a NON-error result noting the pending state + the execution handle (the
    request never blocks on human latency)."""
    if status == "succeeded":
        return {"content": [{"type": "text", "text": output or ""}], "isError": False}
    if status == "pending":
        text = (
            "A human step is pending for this request; the result is not yet available "
            f"(execution {execution_id})."
        )
        return {"content": [{"type": "text", "text": text}], "isError": False}
    return {
        "content": [{"type": "text", "text": "The agent run did not complete successfully."}],
        "isError": True,
    }
