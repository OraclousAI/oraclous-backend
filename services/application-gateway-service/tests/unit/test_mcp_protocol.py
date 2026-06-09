"""Unit: the pure MCP/JSON-RPC protocol helpers (R6 Slice 8). No I/O."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.domain.mcp_protocol import (
    PROTOCOL_VERSION,
    call_result_from_invoke,
    initialize_result,
    jsonrpc_error,
    jsonrpc_result,
    tool_descriptor,
)

pytestmark = pytest.mark.unit


def test_jsonrpc_envelopes() -> None:
    assert jsonrpc_result(1, {"ok": True}) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    err = jsonrpc_error("x", -32601, "method not found")
    assert err == {
        "jsonrpc": "2.0",
        "id": "x",
        "error": {"code": -32601, "message": "method not found"},
    }


def test_initialize_advertises_tools_only() -> None:
    r = initialize_result()
    assert r["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in r["capabilities"] and "serverInfo" in r


def test_tool_descriptor_has_a_fixed_input_schema() -> None:
    t = tool_descriptor(slug="weather", display_name="Weather", description="Forecast")
    assert t["name"] == "weather" and t["description"] == "Forecast"
    assert t["inputSchema"]["required"] == ["input"]
    assert t["inputSchema"]["properties"]["input"]["type"] == "string"


def test_call_result_mapping() -> None:
    ok = call_result_from_invoke(status="succeeded", output="42", execution_id="e1")
    assert ok == {"content": [{"type": "text", "text": "42"}], "isError": False}
    fail = call_result_from_invoke(status="failed", output=None, execution_id="e2")
    assert fail["isError"] is True and "not complete" in fail["content"][0]["text"]
    pend = call_result_from_invoke(status="pending", output=None, execution_id="e3")
    # pending is NOT an error; it carries the execution handle so the client can follow up
    assert pend["isError"] is False and "e3" in pend["content"][0]["text"]
