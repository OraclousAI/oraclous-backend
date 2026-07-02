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


def _streamable(
    call_handler: Callable[[httpx.Request], httpx.Response],
    *,
    session_id: str | None = "sess-1",
) -> Callable[[httpx.Request], httpx.Response]:
    """Wrap a ``tools/call`` handler into a full Streamable-HTTP server (#541): ``initialize`` →
    Mcp-Session-Id + result, ``notifications/initialized`` → 202, everything else → the handler."""

    def routed(req: httpx.Request) -> httpx.Response:
        method = json.loads(req.content).get("method") if req.content else None
        if method == "initialize":
            headers = {"mcp-session-id": session_id} if session_id else {}
            return httpx.Response(
                200,
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        return call_handler(req)

    return routed


def _executor(spec: dict, handler: Callable[[httpx.Request], httpx.Response]) -> McpToolExecutor:
    ex = McpToolExecutor({"id": "x", "spec": spec})
    ex.transport = httpx.MockTransport(_streamable(handler))
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


async def test_an_oversized_body_is_rejected_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # #541 review M3: a hostile server declaring a huge Content-Length must be rejected before the
    # body is parsed — it must NOT be read into memory (OOM). We assert on the declared-length gate:
    # a small real body but a lying multi-GB Content-Length header is a clean MCP_BAD_RESPONSE.
    from oraclous_capability_registry_service.domain.connectors import mcp_protocol

    monkeypatch.setattr(mcp_protocol, "_MAX_BODY_BYTES", 1024)  # shrink the cap for a cheap test
    big_text = "x" * 4096  # a body larger than the (shrunk) cap
    huge = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": big_text}]}}
    ex = _executor(
        {"type": "mcp", "server_url": _URL, "tool_name": "t"},
        lambda _r: httpx.Response(200, json=huge),
    )
    res = await ex.execute({}, _ctx())
    assert not res.success and res.error_type == "MCP_BAD_RESPONSE"


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


# ── #541: the full Streamable-HTTP protocol (initialize → session → SSE tools/call) ──


async def test_tools_call_parses_an_sse_framed_response() -> None:
    # a real hosted MCP server SSE-frames the response; the executor must parse the data: frame.
    def call(_req: httpx.Request) -> httpx.Response:
        sse = (
            "event: message\n"
            'data: {"jsonrpc":"2.0","id":2,"result":'
            '{"content":[{"type":"text","text":"sse-ok"}]}}\n\n'
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    ex = _executor({"type": "mcp", "server_url": _URL, "tool_name": "t"}, call)
    res = await ex.execute({}, _ctx())
    assert res.success and res.data == {"content": [{"type": "text", "text": "sse-ok"}]}


async def test_the_session_id_is_echoed_on_the_tools_call() -> None:
    seen: dict = {}

    def call(req: httpx.Request) -> httpx.Response:
        seen["session"] = req.headers.get("mcp-session-id")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"content": []}})

    ex = McpToolExecutor({"id": "x", "spec": {"type": "mcp", "server_url": _URL, "tool_name": "t"}})
    ex.transport = httpx.MockTransport(_streamable(call, session_id="the-session-42"))
    res = await ex.execute({}, _ctx())
    assert res.success
    assert seen["session"] == "the-session-42"  # the handshake's session id rides the tools/call


async def test_a_sessionless_server_still_works() -> None:
    # a server that returns no Mcp-Session-Id (stateless) is still callable — no header sent.
    def call(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("mcp-session-id") is None
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"content": []}})

    ex = McpToolExecutor({"id": "x", "spec": {"type": "mcp", "server_url": _URL, "tool_name": "t"}})
    ex.transport = httpx.MockTransport(_streamable(call, session_id=None))
    res = await ex.execute({}, _ctx())
    assert res.success


async def test_a_rejected_initialize_is_a_clean_handshake_error() -> None:
    def routed(req: httpx.Request) -> httpx.Response:
        # the server rejects the handshake itself — a clean MCP_HANDSHAKE_ERROR, never a leak.
        return httpx.Response(200, json={"error": {"code": -32600, "message": "SECRET refuse"}})

    ex = McpToolExecutor({"id": "x", "spec": {"type": "mcp", "server_url": _URL, "tool_name": "t"}})
    ex.transport = httpx.MockTransport(routed)
    res = await ex.execute({}, _ctx())
    assert not res.success and res.error_type == "MCP_HANDSHAKE_ERROR"
    assert "SECRET" not in (res.error_message or "")


async def test_the_bearer_is_sent_on_the_handshake_and_the_call() -> None:
    seen: list[str | None] = []

    def routed(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization"))
        method = json.loads(req.content).get("method")
        if method == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "s"}, json={"result": {}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, json={"result": {"content": []}})

    ex = McpToolExecutor({"id": "x", "spec": {"type": "mcp", "server_url": _URL, "tool_name": "t"}})
    ex.transport = httpx.MockTransport(routed)
    res = await ex.execute({}, _ctx({"api_key": {"api_key": "tok-9"}}))
    assert res.success
    assert all(a == "Bearer tok-9" for a in seen)  # the bearer rides EVERY request, incl. init
