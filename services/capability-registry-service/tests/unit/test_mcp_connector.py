"""Unit: the McpToolExecutor — egress guard, broker auth, tools/call, no-leak (R6 MCP)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import httpx
import pytest
from oraclous_capability_registry_service.domain.connectors.mcp import McpToolExecutor
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit

_URL = "https://93.184.216.34/rpc"  # a literal PUBLIC ip → egress allowed without a DNS lookup


def _ctx(credentials: dict | None = None) -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        credentials=credentials or {},
    )


def _executor(spec: dict, handler: Callable[[httpx.Request], httpx.Response]) -> McpToolExecutor:
    ex = McpToolExecutor({"id": "x", "spec": spec})
    ex.transport = httpx.MockTransport(handler)
    return ex


async def test_successful_tools_call_returns_content_and_sends_the_bearer() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "hi"}]},
            },
        )

    ex = _executor({"type": "mcp", "server_url": _URL, "tool_name": "do_thing"}, handler)
    res = await ex.execute({"q": "x"}, _ctx({"api_key": {"api_key": "k-123"}}))
    assert res.success and res.data == {"content": [{"type": "text", "text": "hi"}]}
    assert seen["auth"] == "Bearer k-123"  # the broker-resolved key is sent as a Bearer
    assert seen["body"]["method"] == "tools/call"
    assert seen["body"]["params"]["name"] == "do_thing"
    assert seen["body"]["params"]["arguments"] == {"q": "x"}


async def test_an_internal_target_is_blocked_before_any_call() -> None:
    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    ex = _executor(
        {"type": "mcp", "server_url": "http://169.254.169.254/", "tool_name": "t"}, handler
    )
    res = await ex.execute({}, _ctx())
    assert not res.success and res.error_type == "EGRESS_BLOCKED"
    assert called["n"] == 0  # never reached the network


async def test_tool_iserror_is_a_failure() -> None:
    ex = _executor(
        {"type": "mcp", "server_url": _URL, "tool_name": "t"},
        lambda _r: httpx.Response(200, json={"result": {"isError": True, "content": []}}),
    )
    res = await ex.execute({}, _ctx())
    assert not res.success and res.error_type == "MCP_TOOL_ERROR"


async def test_a_jsonrpc_error_never_leaks_the_raw_message() -> None:
    ex = _executor(
        {"type": "mcp", "server_url": _URL, "tool_name": "t"},
        lambda _r: httpx.Response(
            200, json={"error": {"code": -32000, "message": "SECRET internal detail"}}
        ),
    )
    res = await ex.execute({}, _ctx())
    assert not res.success and res.error_type == "MCP_TOOL_ERROR"
    assert "SECRET" not in (res.error_message or "")  # raw upstream error never surfaced
    assert res.metadata.get("code") == -32000  # only the coarse code is exposed


async def test_non_200_is_an_http_error() -> None:
    ex = _executor(
        {"type": "mcp", "server_url": _URL, "tool_name": "t"},
        lambda _r: httpx.Response(503),
    )
    res = await ex.execute({}, _ctx())
    assert not res.success and res.error_type == "MCP_HTTP_ERROR"


async def test_an_incomplete_spec_is_rejected() -> None:
    ex = _executor({"type": "mcp"}, lambda _r: httpx.Response(200))  # no server_url/tool_name
    res = await ex.execute({}, _ctx())
    assert not res.success and res.error_type == "INVALID_SPEC"


async def test_no_bearer_is_sent_when_there_is_no_credential() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"result": {"content": []}})

    ex = _executor({"type": "mcp", "server_url": _URL, "tool_name": "t"}, handler)
    res = await ex.execute({}, _ctx())  # no credentials in context
    assert res.success and seen["auth"] is None


@pytest.mark.parametrize("body", [[1, 2, 3], "a bare string", 42, {"result": "not-an-object"}])
async def test_a_malformed_non_dict_body_is_a_clean_bad_response(body: object) -> None:
    # a hostile server returning a non-dict JSON value must NOT crash the parser into an Attr error
    # whose text leaks the internal exception — it is a clean MCP_BAD_RESPONSE.
    ex = _executor(
        {"type": "mcp", "server_url": _URL, "tool_name": "t"},
        lambda _r: httpx.Response(200, json=body),
    )
    res = await ex.execute({}, _ctx())
    assert not res.success and res.error_type == "MCP_BAD_RESPONSE"
    assert "has no attribute" not in (res.error_message or "")  # no leaked AttributeError text


# ── #492: the connector DIALS the pinned vetted IP (closes the DNS-rebinding TOCTOU) ──────────────


async def test_mcp_dials_the_pinned_ip_and_preserves_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a hostname server_url: the guard resolves+vets ONE IP; the connector must connect to THAT IP
    # (not re-resolve the name), preserving the Host header + TLS SNI = the original hostname.
    from oraclous_capability_registry_service.domain import egress as egress_mod

    async def fake_egress(url: str, **_kw: object) -> str | None:
        return "93.184.216.34"  # the pinned vetted IP for mcp.example.com

    monkeypatch.setattr(egress_mod, "egress_allowed", fake_egress)
    monkeypatch.setattr(
        "oraclous_capability_registry_service.domain.connectors.mcp.egress_allowed", fake_egress
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["host"] = req.url.host
        seen["host_header"] = req.headers.get("host")
        seen["sni"] = req.extensions.get("sni_hostname")
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": "ok"}]}})

    ex = _executor(
        {"type": "mcp", "server_url": "https://mcp.example.com/rpc", "tool_name": "t"}, handler
    )
    res = await ex.execute({}, _ctx())
    assert res.success
    assert seen["host"] == "93.184.216.34"  # dialed the PINNED IP, not re-resolved the name
    assert seen["host_header"] == "mcp.example.com"  # original host preserved for routing
    assert seen["sni"] == "mcp.example.com"  # TLS SNI targets the real name (cert validation)


async def test_mcp_refuses_when_the_guard_pins_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # egress_allowed → None (a rebinding host resolving inward): no call is made, EGRESS_BLOCKED.
    called = {"n": 0}

    async def fake_egress(url: str, **_kw: object) -> str | None:
        return None

    monkeypatch.setattr(
        "oraclous_capability_registry_service.domain.connectors.mcp.egress_allowed", fake_egress
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    ex = _executor(
        {"type": "mcp", "server_url": "https://rebind.example.com/rpc", "tool_name": "t"}, handler
    )
    res = await ex.execute({}, _ctx())
    assert not res.success and res.error_type == "EGRESS_BLOCKED"
    assert called["n"] == 0  # never reached the network
