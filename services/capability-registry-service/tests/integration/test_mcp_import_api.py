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


def _mcp_handler(_request: httpx.Request) -> httpx.Response:
    """A stub MCP server exposing two tools via ``tools/list`` (no real network)."""
    return httpx.Response(
        200,
        json={"result": {"tools": [{"name": "do_a", "description": "A"}, {"name": "do_b"}]}},
    )


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", _INTERNAL_KEY)
    monkeypatch.setenv("AUTH_MODE", "gateway")
    monkeypatch.setenv("PLATFORM_ORG_ID", _PLATFORM_ORG)
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
    from oraclous_capability_registry_service.services.mcp_import_service import McpImportService

    app = create_app(lifespan=None)
    repo = CapabilityRepository(async_dsn, platform_org_id=_uuid.UUID(_PLATFORM_ORG))
    app.state.capability_repository = repo

    # Inject the MockTransport into the import service so import-mcp never hits the network.
    def _mock_import_service() -> McpImportService:
        return McpImportService(capabilities=repo, transport=httpx.MockTransport(_mcp_handler))

    app.dependency_overrides[get_mcp_import_service] = _mock_import_service

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        yield c
    await repo.close()
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
    assert names == {"acme/do_a", "acme/do_b"}

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
