"""Unit: McpService — the JSON-RPC dispatcher, tools/list scoping, tools/call mapping. No DB."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from oraclous_application_gateway_service.services.integration_key_auth_service import ResolvedKey
from oraclous_application_gateway_service.services.invoke_service import (
    AgentNotFound,
    UpstreamInvokeError,
)
from oraclous_application_gateway_service.services.mcp_service import McpService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()


def _agent(slug: str, cap: str, status: str = "active"):  # noqa: ANN202
    return SimpleNamespace(
        slug=slug, bound_capability_ref=cap, display_name=slug, description=None, status=status
    )


class _Agents:
    def __init__(self, rows) -> None:  # noqa: ANN001
        self._rows = rows

    async def get_by_slug(self, *, organisation_id, slug):  # noqa: ANN001
        return next((a for a in self._rows if a.slug == slug and organisation_id == _ORG), None)

    async def list_for_org(self, organisation_id):  # noqa: ANN001
        return list(self._rows) if organisation_id == _ORG else []


class _Invoke:
    def __init__(self, *, result=None, raises: Exception | None = None) -> None:  # noqa: ANN001
        self._result = result
        self._raises = raises
        self.calls: list = []

    async def invoke(self, *, slug, agent_input, principal):  # noqa: ANN001
        self.calls.append((slug, agent_input))
        if self._raises is not None:
            raise self._raises
        return self._result


def _key(*, bound: str | None = None, allow: list[str] | None = None) -> ResolvedKey:
    return ResolvedKey(
        principal=Principal(
            principal_id=uuid.uuid4(),
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            organisation_id=_ORG,
        ),
        key_id=uuid.uuid4(),
        bound_agent_slug=bound,
        capability_allow_list=allow,
        cors_origins=None,
    )


def _svc(agents, invoke=None) -> McpService:  # noqa: ANN001
    return McpService(agents=agents, invoke=invoke or _Invoke())


# ── envelope / methods ──────────────────────────────────────────────────────────────────────────
async def test_initialize() -> None:
    r = await _svc(_Agents([])).dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, _key(bound="x")
    )
    assert r["result"]["protocolVersion"] and r["id"] == 1


async def test_a_notification_gets_no_response() -> None:
    r = await _svc(_Agents([])).dispatch(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, _key(bound="x")
    )
    assert r is None


async def test_unknown_method_is_method_not_found() -> None:
    r = await _svc(_Agents([])).dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list"}, _key(bound="x")
    )
    assert r["error"]["code"] == -32601


async def test_malformed_envelope_is_invalid_request() -> None:
    r = await _svc(_Agents([])).dispatch({"id": 3, "method": "x"}, _key(bound="x"))
    assert r["error"]["code"] == -32600


# ── tools/list scoping ──────────────────────────────────────────────────────────────────────────
async def test_bound_key_lists_exactly_its_one_agent() -> None:
    agents = _Agents([_agent("weather", "cap-w"), _agent("news", "cap-n")])
    r = await _svc(agents).dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, _key(bound="weather")
    )
    names = [t["name"] for t in r["result"]["tools"]]
    assert names == ["weather"]


async def test_cap_allow_list_key_lists_only_allowed_active_agents() -> None:
    agents = _Agents(
        [
            _agent("weather", "cap-w"),
            _agent("news", "cap-n"),
            _agent("old", "cap-o", status="revoked"),
        ]
    )
    r = await _svc(agents).dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, _key(allow=["cap-w"])
    )
    assert [t["name"] for t in r["result"]["tools"]] == ["weather"]


async def test_a_key_with_no_binding_lists_nothing() -> None:
    agents = _Agents([_agent("weather", "cap-w")])
    r = await _svc(agents).dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, _key())
    assert r["result"]["tools"] == []


# ── tools/call ──────────────────────────────────────────────────────────────────────────────────
async def test_call_success_returns_content() -> None:
    agents = _Agents([_agent("weather", "cap-w")])
    inv = _Invoke(
        result=SimpleNamespace(status="succeeded", output="sunny", execution_id=uuid.uuid4())
    )
    r = await _svc(agents, inv).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "weather", "arguments": {"input": "today?"}},
        },
        _key(bound="weather"),
    )
    assert r["result"]["content"][0]["text"] == "sunny" and r["result"]["isError"] is False
    assert inv.calls == [("weather", "today?")]


async def test_call_a_tool_outside_the_binding_is_unknown_tool_no_invoke() -> None:
    agents = _Agents([_agent("weather", "cap-w"), _agent("secret", "cap-s")])
    inv = _Invoke()
    r = await _svc(agents, inv).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "secret", "arguments": {"input": "x"}},
        },
        _key(bound="weather"),
    )
    assert r["error"]["code"] == -32602 and "unknown tool" in r["error"]["message"]
    assert inv.calls == []  # never invoked


async def test_call_missing_input_is_invalid_params() -> None:
    agents = _Agents([_agent("weather", "cap-w")])
    r = await _svc(agents).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "weather", "arguments": {}},
        },
        _key(bound="weather"),
    )
    assert r["error"]["code"] == -32602


async def test_call_upstream_failure_is_a_tool_error_not_a_leak() -> None:
    agents = _Agents([_agent("weather", "cap-w")])
    inv = _Invoke(raises=UpstreamInvokeError("harness returned 502: sk-secret"))
    r = await _svc(agents, inv).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "weather", "arguments": {"input": "x"}},
        },
        _key(bound="weather"),
    )
    assert r["result"]["isError"] is True
    assert "sk-secret" not in r["result"]["content"][0]["text"]  # no raw leak


async def test_call_agent_not_found_is_unknown_tool() -> None:
    agents = _Agents([_agent("weather", "cap-w")])
    inv = _Invoke(raises=AgentNotFound("weather"))
    r = await _svc(agents, inv).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "weather", "arguments": {"input": "x"}},
        },
        _key(bound="weather"),
    )
    assert r["error"]["code"] == -32602


async def test_call_non_dict_params_does_not_crash() -> None:
    inv = _Invoke()
    r = await _svc(_Agents([_agent("weather", "cap-w")]), inv).dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1, 2]},
        _key(bound="weather"),
    )
    assert r["error"]["code"] == -32602 and inv.calls == []  # a clean error, never an invoke/500


async def test_call_an_unpublished_bound_agent_is_unknown_tool() -> None:
    # the bound agent is revoked -> omitted from tools/list AND not callable (no invoke reached)
    agents = _Agents([_agent("weather", "cap-w", status="revoked")])
    inv = _Invoke()
    r = await _svc(agents, inv).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "weather", "arguments": {"input": "x"}},
        },
        _key(bound="weather"),
    )
    assert r["error"]["code"] == -32602 and "unknown tool" in r["error"]["message"]
    assert inv.calls == []
