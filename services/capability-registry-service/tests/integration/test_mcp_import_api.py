"""Integration: MCP import/approve HITL gate vs real Postgres (R6 MCP-import, #233).

End-to-end over the real registry endpoints + real Postgres:
  - an org ADMIN's ``POST /api/v1/tools/import-mcp`` lands the discovered tools as
    ``pending_approval`` (the supply-chain HITL gate) and they show up that way in the catalogue;
  - a plain MEMBER is FORBIDDEN (403) from import AND from approve;
  - an admin's ``POST /api/v1/tools/{id}/approve`` flips the tool to ``active`` (executable);
  - approve of an unknown / cross-org id is masked as 404.

The external MCP ``tools/list`` call is served by an injected ``httpx.MockTransport`` (same stubbing
the unit suite uses) — no real network. Runs in ``gateway`` mode so each caller supplies its own
verified ``X-Principal-Org-Role`` (ADR-018); admin vs member is the only difference.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_TENANT_A = "00000000-0000-0000-0000-00000000aaaa"
_TENANT_B = "00000000-0000-0000-0000-00000000bbbb"
_PRINCIPAL = "00000000-0000-0000-0000-0000000000c5"
_INTERNAL_KEY = "dev-internal-key"
_PLATFORM_ORG = "00000000-0000-0000-0000-0000000000a0"
_PUB_MCP = "https://93.184.216.34/mcp"  # a literal PUBLIC ip → egress allowed without a DNS lookup


_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "owner": {"type": "string"},
        "filters": {"type": "object", "properties": {"paths": {"type": "array"}}},
    },
    "required": ["owner"],
}


def _mcp_handler(_request: httpx.Request) -> httpx.Response:
    """A stub MCP server exposing two tools via ``tools/list`` (no real network). #698 D1: a real
    server declares an ``inputSchema`` per tool, and ``do_b`` declares none (also legal MCP)."""
    return httpx.Response(
        200,
        json={
            "result": {
                "tools": [
                    {"name": "do_a", "description": "A", "inputSchema": _INPUT_SCHEMA},
                    {"name": "do_b"},
                ]
            }
        },
    )


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", _INTERNAL_KEY)
    monkeypatch.setenv("AUTH_MODE", "gateway")
    monkeypatch.setenv("PLATFORM_ORG_ID", _PLATFORM_ORG)
    monkeypatch.setenv("CREDENTIAL_BROKER_MODE", "fake")  # #698: the execute leg needs a broker
    from oraclous_capability_registry_service.core.config import get_settings

    get_settings.cache_clear()

    import uuid as _uuid

    from oraclous_capability_registry_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from oraclous_capability_registry_service.app.factory import create_app
    from oraclous_capability_registry_service.core.dependencies import get_mcp_import_service
    from oraclous_capability_registry_service.repositories.capability_repository import (
        CapabilityRepository,
    )
    from oraclous_capability_registry_service.repositories.execution_repository import (
        ExecutionRepository,
    )
    from oraclous_capability_registry_service.repositories.instance_repository import (
        InstanceRepository,
    )
    from oraclous_capability_registry_service.services.credential_client import (
        FakeCredentialBroker,
        _libpq_dsn,
    )
    from oraclous_capability_registry_service.services.mcp_import_service import McpImportService

    app = create_app(lifespan=None)
    repo = CapabilityRepository(async_dsn, platform_org_id=_uuid.UUID(_PLATFORM_ORG))
    # #698: an imported tool is only worth importing if it can then be INSTANCED and EXECUTED, so
    # this fixture now wires the instance + execution repositories the execute path needs.
    inst_repo = InstanceRepository(async_dsn)
    exec_repo = ExecutionRepository(async_dsn)
    app.state.capability_repository = repo
    app.state.instance_repository = inst_repo
    app.state.execution_repository = exec_repo
    app.state.credential_broker = FakeCredentialBroker(fake_db_dsn=_libpq_dsn(async_dsn))

    # Inject the MockTransport into the import service so import-mcp never hits the network.
    def _mock_import_service() -> McpImportService:
        return McpImportService(capabilities=repo, transport=httpx.MockTransport(_mcp_handler))

    app.dependency_overrides[get_mcp_import_service] = _mock_import_service

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        yield c
    await repo.close()
    await inst_repo.close()
    await exec_repo.close()
    get_settings.cache_clear()


def _auth(*, role: str, org: str = _TENANT_A) -> dict:
    """Gateway-mode identity headers carrying a trust-asserted org role (ADR-018 / R7-SEC S2)."""
    return {
        "X-Internal-Key": _INTERNAL_KEY,
        "X-Principal-Id": _PRINCIPAL,
        "X-Principal-Type": "user",
        "X-Organisation-Id": org,
        "X-Principal-Org-Role": role,
    }


async def test_admin_import_lands_tools_pending_approval(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/tools/import-mcp",
        json={"server_url": _PUB_MCP, "label": "acme"},
        headers=_auth(role="admin"),
    )
    assert resp.status_code == 201, resp.text
    imported = resp.json()["imported"]
    assert len(imported) == 2
    assert all(t["status"] == "pending_approval" for t in imported)
    names = {t["name"] for t in imported}
    assert names == {"acme-do_a", "acme-do_b"}  # #698 D4: no "/" — see the D4 tests below

    # they show up pending in the tenant's catalogue too
    listed = (await client.get("/api/v1/tools", headers=_auth(role="admin"))).json()
    mcp_tools = [t for t in listed["capabilities"] if t["descriptor"]["spec"].get("type") == "mcp"]
    assert len(mcp_tools) == 2
    assert all(t["status"] == "pending_approval" for t in mcp_tools)


async def test_member_cannot_import(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/tools/import-mcp",
        json={"server_url": _PUB_MCP, "label": "acme"},
        headers=_auth(role="member"),
    )
    assert resp.status_code == 403, resp.text


async def test_admin_approve_flips_to_active(client: AsyncClient) -> None:
    imported = (
        await client.post(
            "/api/v1/tools/import-mcp",
            json={"server_url": _PUB_MCP, "label": "acme"},
            headers=_auth(role="admin"),
        )
    ).json()["imported"]
    tid = imported[0]["id"]
    assert imported[0]["status"] == "pending_approval"

    approved = await client.post(f"/api/v1/tools/{tid}/approve", headers=_auth(role="admin"))
    assert approved.status_code == 204, approved.text

    got = (await client.get(f"/api/v1/tools/{tid}", headers=_auth(role="admin"))).json()
    assert got["status"] == "active"


async def test_member_cannot_approve(client: AsyncClient) -> None:
    tid = (
        await client.post(
            "/api/v1/tools/import-mcp",
            json={"server_url": _PUB_MCP, "label": "acme"},
            headers=_auth(role="admin"),
        )
    ).json()["imported"][0]["id"]

    resp = await client.post(f"/api/v1/tools/{tid}/approve", headers=_auth(role="member"))
    assert resp.status_code == 403, resp.text
    # still pending after the forbidden attempt
    got = (await client.get(f"/api/v1/tools/{tid}", headers=_auth(role="admin"))).json()
    assert got["status"] == "pending_approval"


async def test_approve_unknown_id_is_404(client: AsyncClient) -> None:
    unknown = "00000000-0000-0000-0000-0000deadbeef"
    resp = await client.post(f"/api/v1/tools/{unknown}/approve", headers=_auth(role="admin"))
    assert resp.status_code == 404, resp.text


async def test_admin_reject_flips_to_rejected(client: AsyncClient) -> None:
    imported = (
        await client.post(
            "/api/v1/tools/import-mcp",
            json={"server_url": _PUB_MCP, "label": "acme"},
            headers=_auth(role="admin"),
        )
    ).json()["imported"]
    tid = imported[0]["id"]
    assert imported[0]["status"] == "pending_approval"

    rejected = await client.post(f"/api/v1/tools/{tid}/reject", headers=_auth(role="admin"))
    assert rejected.status_code == 204, rejected.text

    got = (await client.get(f"/api/v1/tools/{tid}", headers=_auth(role="admin"))).json()
    assert got["status"] == "rejected"


async def test_member_cannot_reject(client: AsyncClient) -> None:
    tid = (
        await client.post(
            "/api/v1/tools/import-mcp",
            json={"server_url": _PUB_MCP, "label": "acme"},
            headers=_auth(role="admin"),
        )
    ).json()["imported"][0]["id"]

    resp = await client.post(f"/api/v1/tools/{tid}/reject", headers=_auth(role="member"))
    assert resp.status_code == 403, resp.text
    # still pending after the forbidden attempt
    got = (await client.get(f"/api/v1/tools/{tid}", headers=_auth(role="admin"))).json()
    assert got["status"] == "pending_approval"


async def test_reject_unknown_id_is_404(client: AsyncClient) -> None:
    unknown = "00000000-0000-0000-0000-0000deadbeef"
    resp = await client.post(f"/api/v1/tools/{unknown}/reject", headers=_auth(role="admin"))
    assert resp.status_code == 404, resp.text


async def test_reject_an_already_approved_tool_is_404(client: AsyncClient) -> None:
    # an active (approved) tool is past the gate — the reject route only declines pending tools.
    tid = (
        await client.post(
            "/api/v1/tools/import-mcp",
            json={"server_url": _PUB_MCP, "label": "acme"},
            headers=_auth(role="admin"),
        )
    ).json()["imported"][0]["id"]
    assert (
        await client.post(f"/api/v1/tools/{tid}/approve", headers=_auth(role="admin"))
    ).status_code == 204

    resp = await client.post(f"/api/v1/tools/{tid}/reject", headers=_auth(role="admin"))
    assert resp.status_code == 404, resp.text
    # unchanged — still active
    got = (await client.get(f"/api/v1/tools/{tid}", headers=_auth(role="admin"))).json()
    assert got["status"] == "active"


# ── #698: import → approve → EXECUTE, the leg that was never joined ───────────────────────────────
#
# Everything above proves the import and approval gate. None of it proves a member can CALL the
# tool, which is the whole point of importing one. These tests drive the remaining leg against a
# real Postgres and a stub MCP server, and assert what the stub actually received.


def _tools_call_stub(seen: dict) -> httpx.MockTransport:
    """A stub MCP server that records the ``tools/call`` it receives and answers with content."""
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content) if request.content else {}
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200, headers={"mcp-session-id": "s-1"}, json={"result": {"capabilities": {}}}
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return _mcp_handler(request)
        seen["method"] = method
        seen["params"] = body.get("params")
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": "ok"}]}})

    return httpx.MockTransport(handler)


async def _import_and_approve(client: AsyncClient) -> dict:
    imported = (
        await client.post(
            "/api/v1/tools/import-mcp",
            json={"server_url": _PUB_MCP, "label": "acme"},
            headers=_auth(role="admin"),
        )
    ).json()["imported"]
    tool = next(t for t in imported if t["name"].endswith("do_a"))
    approved = await client.post(f"/api/v1/tools/{tool['id']}/approve", headers=_auth(role="admin"))
    assert approved.status_code == 204, approved.text
    return tool


async def test_the_imported_descriptor_carries_the_discovered_schema(client: AsyncClient) -> None:
    """D1 through the real API and a real Postgres round-trip: the nested ``inputSchema`` survives
    the JSONB write and comes back unchanged, so the runtime can offer it to the model."""
    tool = await _import_and_approve(client)
    got = (await client.get(f"/api/v1/tools/{tool['id']}", headers=_auth(role="admin"))).json()
    ops = got["descriptor"]["spec"]["capabilities"]
    assert [o["name"] for o in ops] == ["do_a"]
    assert ops[0]["parameters_schema"] == _INPUT_SCHEMA


async def test_a_tool_with_no_declared_schema_still_gets_an_operation(client: AsyncClient) -> None:
    imported = (
        await client.post(
            "/api/v1/tools/import-mcp",
            json={"server_url": _PUB_MCP, "label": "acme"},
            headers=_auth(role="admin"),
        )
    ).json()["imported"]
    tool = next(t for t in imported if t["name"].endswith("do_b"))
    got = (await client.get(f"/api/v1/tools/{tool['id']}", headers=_auth(role="admin"))).json()
    assert [o["name"] for o in got["descriptor"]["spec"]["capabilities"]] == ["do_b"]


async def test_the_stored_name_has_no_slash_and_keeps_the_label(client: AsyncClient) -> None:
    """D4 across the real column: ``name`` is what capability resolution matches against."""
    tool = await _import_and_approve(client)
    assert "/" not in tool["name"]
    got = (await client.get(f"/api/v1/tools/{tool['id']}", headers=_auth(role="admin"))).json()
    assert got["name"] == "acme-do_a"  # the denormalised COLUMN, not just the JSONB
    assert got["descriptor"]["spec"]["label"] == "acme"


async def test_an_approved_tool_executes_and_the_server_sees_clean_arguments(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3 end to end: the loop dispatches ``{"operation": ..., **args}`` and the external server
    must receive ONLY the arguments. This is the call that a schema-validating server rejects."""
    from oraclous_capability_registry_service.domain.connectors import mcp as mcp_mod

    seen: dict = {}
    monkeypatch.setattr(mcp_mod.McpToolExecutor, "transport", _tools_call_stub(seen))

    tool = await _import_and_approve(client)
    iid = (
        await client.post(
            "/api/v1/instances",
            json={"capability_id": tool["id"], "name": "acme-do_a"},
            headers=_auth(role="admin"),
        )
    ).json()["id"]

    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "do_a", "owner": "acme"}},
        headers=_auth(role="admin"),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "SUCCESS", resp.text
    assert seen["method"] == "tools/call"
    assert seen["params"]["name"] == "do_a"
    assert seen["params"]["arguments"] == {"owner": "acme"}
    assert "operation" not in seen["params"]["arguments"]


async def test_a_pending_tool_is_still_not_executable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supply-chain gate survives everything #698 changes — approval is still required."""
    from oraclous_capability_registry_service.domain.connectors import mcp as mcp_mod

    seen: dict = {}
    monkeypatch.setattr(mcp_mod.McpToolExecutor, "transport", _tools_call_stub(seen))

    tool = (
        await client.post(
            "/api/v1/tools/import-mcp",
            json={"server_url": _PUB_MCP, "label": "acme"},
            headers=_auth(role="admin"),
        )
    ).json()["imported"][0]
    iid = (
        await client.post(
            "/api/v1/instances",
            json={"capability_id": tool["id"], "name": "pending-one"},
            headers=_auth(role="admin"),
        )
    ).json()["id"]

    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "do_a"}},
        headers=_auth(role="admin"),
    )
    assert resp.status_code not in (200, 201), resp.text
    assert seen == {}  # the external server was never contacted
