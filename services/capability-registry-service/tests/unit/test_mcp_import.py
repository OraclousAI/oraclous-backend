"""Unit: McpImportService — discover → pending descriptors, egress, approve, no-leak (R6 MCP)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from oraclous_capability_registry_service.services.mcp_import_service import (
    ACTIVE,
    PENDING,
    REJECTED,
    McpEgressBlocked,
    McpImportError,
    McpImportService,
)

pytestmark = pytest.mark.unit

_PUB = "https://93.184.216.34/mcp"  # a literal PUBLIC ip → egress allowed without a DNS lookup


class _FakeCaps:
    def __init__(self) -> None:
        self.created: list = []
        self.statuses: dict = {}

    async def create(self, *, organisation_id, kind, descriptor, status="active"):  # noqa: ANN001, ANN202
        row = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            kind=kind,
            descriptor=descriptor,
            status=status,
        )
        self.created.append(row)
        return row

    async def set_status(self, *, descriptor_id, organisation_id, status):  # noqa: ANN001, ANN202
        self.statuses[descriptor_id] = status
        return True

    async def set_status_if(  # noqa: ANN202
        self,
        *,
        descriptor_id,
        organisation_id,
        expected,
        status,  # noqa: ANN001
    ):
        # Conditional flip: only transition when the recorded status matches ``expected``.
        if self.statuses.get(descriptor_id, PENDING) != expected:
            return False
        self.statuses[descriptor_id] = status
        return True


def _svc(caps: _FakeCaps, handler) -> McpImportService:  # noqa: ANN001
    return McpImportService(capabilities=caps, transport=httpx.MockTransport(handler))


async def test_import_registers_discovered_tools_as_pending_approval() -> None:
    caps = _FakeCaps()
    handler = lambda _r: httpx.Response(  # noqa: E731
        200,
        json={"result": {"tools": [{"name": "do_a", "description": "A"}, {"name": "do_b"}]}},
    )
    created = await _svc(caps, handler).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="acme"
    )
    assert len(created) == 2 and all(r.status == PENDING for r in created)
    assert created[0].descriptor["spec"] == {
        "type": "mcp",
        "server_url": _PUB,
        "tool_name": "do_a",
    }
    assert created[0].descriptor["metadata"]["name"] == "acme/do_a"


async def test_import_blocks_an_internal_url_before_any_call() -> None:
    called = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    with pytest.raises(McpEgressBlocked):
        await _svc(_FakeCaps(), handler).import_server(
            organisation_id=uuid.uuid4(), server_url="http://169.254.169.254/", label="x"
        )
    assert called["n"] == 0  # never reached the network


@pytest.mark.parametrize("body", [[1, 2], {"result": "x"}, {"no": "result"}, "bare"])
async def test_a_malformed_tools_list_is_a_generic_error(body: object) -> None:
    with pytest.raises(McpImportError):
        await _svc(_FakeCaps(), lambda _r: httpx.Response(200, json=body)).import_server(
            organisation_id=uuid.uuid4(), server_url=_PUB, label="x"
        )


async def test_a_non_200_never_leaks_the_servers_body() -> None:
    with pytest.raises(McpImportError) as exc:
        await _svc(
            _FakeCaps(), lambda _r: httpx.Response(500, text="SECRET stack trace")
        ).import_server(organisation_id=uuid.uuid4(), server_url=_PUB, label="x")
    assert "SECRET" not in str(exc.value)


async def test_approve_flips_status_to_active() -> None:
    caps = _FakeCaps()
    tid, org = uuid.uuid4(), uuid.uuid4()
    ok = await McpImportService(capabilities=caps).approve(descriptor_id=tid, organisation_id=org)
    assert ok and caps.statuses[tid] == ACTIVE


async def test_reject_flips_a_pending_tool_to_rejected() -> None:
    caps = _FakeCaps()
    tid, org = uuid.uuid4(), uuid.uuid4()
    caps.statuses[tid] = PENDING
    ok = await McpImportService(capabilities=caps).reject(descriptor_id=tid, organisation_id=org)
    assert ok and caps.statuses[tid] == REJECTED


async def test_reject_does_not_revert_an_already_active_tool() -> None:
    # the conditional flip protects against declining a tool that was already approved (active).
    caps = _FakeCaps()
    tid, org = uuid.uuid4(), uuid.uuid4()
    caps.statuses[tid] = ACTIVE
    ok = await McpImportService(capabilities=caps).reject(descriptor_id=tid, organisation_id=org)
    assert ok is False and caps.statuses[tid] == ACTIVE


def test_status_for_forces_pending_for_any_mcp_descriptor() -> None:
    # the side-door fix: an MCP tool is pending_approval at creation no matter the path or a passed
    # status (you cannot register an MCP tool directly as active and dodge the HITL gate).
    from oraclous_capability_registry_service.repositories.capability_repository import status_for

    assert status_for({"spec": {"type": "mcp"}}, "active") == "pending_approval"
    assert status_for({"spec": {"type": "mcp"}}, "pending_approval") == "pending_approval"
    # a non-MCP descriptor keeps the requested status (built-ins / first-party stay active)
    assert status_for({"spec": {"type": "database"}}, "active") == "active"
    assert status_for({}, "active") == "active"


# ── #541: auth'd import (credential_id → broker Bearer) + Streamable-HTTP session/SSE ─────────────

from oraclous_capability_registry_service.services.credential_client import (  # noqa: E402
    CredentialResolutionError,
    ResolvedCredential,
)


class _FakeBroker:
    def __init__(
        self, *, api_key: str | None = "imp-tok", raise_exc: Exception | None = None
    ) -> None:
        self._api_key = api_key
        self._raise = raise_exc
        self.seen: dict = {}

    async def resolve(self, *, organisation_id, user_id, requirement, credential_id=None):  # noqa: ANN001, ANN202
        self.seen = {"credential_id": credential_id, "org": organisation_id, "user": user_id}
        if self._raise is not None:
            raise self._raise
        return ResolvedCredential(credential_type="api_key", payload={"api_key": self._api_key})

    async def aclose(self) -> None:  # pragma: no cover
        return None


async def test_import_with_a_credential_resolves_a_bearer_and_records_it() -> None:
    caps, broker = _FakeCaps(), _FakeBroker(api_key="pat-42")
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.setdefault("auth", req.headers.get("authorization"))  # first (initialize) auth header
        return httpx.Response(200, json={"result": {"tools": [{"name": "do_a"}]}})

    svc = McpImportService(capabilities=caps, broker=broker, transport=httpx.MockTransport(handler))
    org, user = uuid.uuid4(), uuid.uuid4()
    created = await svc.import_server(
        organisation_id=org, server_url=_PUB, label="gh", user_id=user, credential_id="cred-1"
    )
    assert seen["auth"] == "Bearer pat-42"  # the broker key rides the handshake + discovery
    assert broker.seen["credential_id"] == "cred-1"
    assert created[0].descriptor["spec"]["credential_id"] == "cred-1"  # invoke path can re-resolve


async def test_import_fails_closed_when_the_credential_is_unresolvable() -> None:
    caps = _FakeCaps()
    broker = _FakeBroker(raise_exc=CredentialResolutionError("no", error_code="not_found"))
    svc = McpImportService(
        capabilities=caps,
        broker=broker,
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"result": {"tools": []}})
        ),
    )
    with pytest.raises(McpImportError):  # fail-closed — never an anonymous fallback
        await svc.import_server(
            organisation_id=uuid.uuid4(),
            server_url=_PUB,
            label="x",
            user_id=uuid.uuid4(),
            credential_id="cred-x",
        )
    assert caps.created == []  # no descriptors created on a fail-closed resolution


async def test_import_parses_an_sse_framed_tools_list() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json

        if _json.loads(req.content).get("method") == "tools/list":
            sse = 'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"sse_tool"}]}}\n\n'
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)
        return httpx.Response(200, json={"result": {}})

    created = await _svc(_FakeCaps(), handler).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="acme"
    )
    assert len(created) == 1 and created[0].descriptor["spec"]["tool_name"] == "sse_tool"
