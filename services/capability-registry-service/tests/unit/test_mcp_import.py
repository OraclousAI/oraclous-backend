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
    spec = created[0].descriptor["spec"]
    assert spec["type"] == "mcp"
    assert spec["server_url"] == _PUB
    assert spec["tool_name"] == "do_a"
    # #698 D4: no "/" in the stored name — see test_the_stored_name_carries_no_slash below.
    assert created[0].descriptor["metadata"]["name"] == "acme-do_a"


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


# ── #698 D1: the discovered inputSchema is STORED, or the model is offered no arguments ───────────
#
# The importer learns each tool's name, description and ``inputSchema`` from ``tools/list`` and
# currently keeps only the name. With no ``spec.capabilities`` the runtime's ``tool_specs_for``
# returns [] and the member is offered no tool at all — the import succeeds and the tool is dead.

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"owner": {"type": "string"}, "pullNumber": {"type": "integer"}},
    "required": ["owner", "pullNumber"],
}


def _tools_list(*tools: dict) -> object:
    return lambda _r: httpx.Response(200, json={"result": {"tools": list(tools)}})


async def test_the_discovered_input_schema_is_stored_as_an_operation() -> None:
    caps = _FakeCaps()
    handler = _tools_list(
        {"name": "pull_request_read", "description": "Read a PR", "inputSchema": _INPUT_SCHEMA}
    )
    created = await _svc(caps, handler).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="github-mcp"
    )
    ops = created[0].descriptor["spec"]["capabilities"]
    assert len(ops) == 1
    assert ops[0]["name"] == "pull_request_read"
    assert ops[0]["description"] == "Read a PR"
    assert ops[0]["parameters_schema"] == _INPUT_SCHEMA  # verbatim — nesting/required intact


async def test_a_tool_without_an_input_schema_still_declares_an_operation() -> None:
    """No ``inputSchema`` is legal MCP. The operation must still exist, or the tool is unreachable
    — it degrades to an empty object schema, never to a missing capability."""
    created = await _svc(_FakeCaps(), _tools_list({"name": "ping"})).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="srv"
    )
    ops = created[0].descriptor["spec"]["capabilities"]
    assert [o["name"] for o in ops] == ["ping"]
    assert ops[0]["parameters_schema"] == {"type": "object", "properties": {}}


@pytest.mark.parametrize("hostile", ["a string", [1, 2], 7, None])
async def test_a_hostile_input_schema_is_replaced_not_stored(hostile: object) -> None:
    """A non-dict ``inputSchema`` from an untrusted server never lands in the JSONB as-is."""
    created = await _svc(
        _FakeCaps(), _tools_list({"name": "t", "inputSchema": hostile})
    ).import_server(organisation_id=uuid.uuid4(), server_url=_PUB, label="srv")
    assert created[0].descriptor["spec"]["capabilities"][0]["parameters_schema"] == {
        "type": "object",
        "properties": {},
    }


async def test_a_hostile_description_is_capped_on_the_operation_too() -> None:
    created = await _svc(
        _FakeCaps(), _tools_list({"name": "t", "description": "D" * 5000})
    ).import_server(organisation_id=uuid.uuid4(), server_url=_PUB, label="srv")
    assert len(created[0].descriptor["spec"]["capabilities"][0]["description"]) <= 500


# ── #698 D2: the import credential is DECLARED, so the invoke path can resolve it ─────────────────
#
# ``spec.credential_id`` is written at import and never read at execute time, so a hosted server
# gets an anonymous ``tools/call`` and answers 401. The importer must additionally DECLARE the
# requirement, because ``ToolExecutionService`` only resolves credentials it finds declared in
# ``spec.credential_requirements``.


async def test_an_authd_import_declares_a_credential_requirement() -> None:
    caps, broker = _FakeCaps(), _FakeBroker()
    svc = McpImportService(
        capabilities=caps, broker=broker, transport=httpx.MockTransport(_tools_list({"name": "t"}))
    )
    created = await svc.import_server(
        organisation_id=uuid.uuid4(),
        server_url=_PUB,
        label="gh",
        user_id=uuid.uuid4(),
        credential_id="cred-1",
    )
    reqs = created[0].descriptor["spec"]["credential_requirements"]
    assert len(reqs) == 1
    assert reqs[0]["type"] == "api_key"
    assert reqs[0]["required"] is True
    assert "provider" in reqs[0]  # the member must learn WHICH credential to onboard


async def test_an_anonymous_import_declares_no_requirement() -> None:
    """A public server needs no key. Declaring one anyway would fail-close every call to it."""
    created = await _svc(_FakeCaps(), _tools_list({"name": "t"})).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="pub"
    )
    spec = created[0].descriptor["spec"]
    assert "credential_requirements" not in spec or spec["credential_requirements"] == []
    assert "credential_id" not in spec


# ── #698 D4: a stored name that BOTH resolves and passes the policy set ───────────────────────────
#
# Two rules read the same ref and split it differently: ``_ref_slug`` keeps the tail after the LAST
# "/", ``_registry_of`` keeps the head before the FIRST "/". A stored ``label/tool`` name makes
# ``org:<id>/label/tool`` the only ref that could name it, and that ref resolves to the slug
# "tool" — which matches nothing. Removing the "/" from the stored name satisfies both rules.
# Shape confirmation is tracked as a Contract on #699.


async def test_the_stored_name_carries_no_slash() -> None:
    created = await _svc(_FakeCaps(), _tools_list({"name": "pull_request_read"})).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="github-mcp"
    )
    name = created[0].descriptor["metadata"]["name"]
    assert "/" not in name
    assert name == "github-mcp-pull_request_read"


async def test_the_label_is_kept_separately_for_display_and_regrouping() -> None:
    """Folding the label into the name loses which server a tool came from. ``spec.label`` keeps
    it, so an admin can still see and regroup an org's tools by their source server."""
    created = await _svc(_FakeCaps(), _tools_list({"name": "t"})).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="github-mcp"
    )
    assert created[0].descriptor["spec"]["label"] == "github-mcp"


async def test_a_label_containing_a_slash_cannot_smuggle_one_into_the_name() -> None:
    """The label is admin-supplied. A "/" in it would reintroduce exactly the defect D4 fixes."""
    created = await _svc(_FakeCaps(), _tools_list({"name": "t"})).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="acme/prod"
    )
    assert "/" not in created[0].descriptor["metadata"]["name"]


async def test_a_tool_name_containing_a_slash_cannot_smuggle_one_either() -> None:
    """The tool name comes from an UNTRUSTED server — the same guard must apply to it."""
    created = await _svc(_FakeCaps(), _tools_list({"name": "a/b"})).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="srv"
    )
    assert "/" not in created[0].descriptor["metadata"]["name"]


async def test_the_stored_name_stays_within_the_column_width() -> None:
    """``name`` is a String(255) column. A hostile 255-char tool name plus a label must not overflow
    it — the row would be rejected at INSERT and the whole import would fail."""
    created = await _svc(_FakeCaps(), _tools_list({"name": "z" * 300})).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="l" * 60
    )
    assert len(created[0].descriptor["metadata"]["name"]) <= 255


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
