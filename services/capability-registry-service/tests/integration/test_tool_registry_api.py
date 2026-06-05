"""Integration: tool registry + global/platform catalogue vs real Postgres (S2).

Proves startup plugin discovery seeds the built-in tool catalogue (deterministic ids) under the
*platform* org → a DIFFERENT tenant org's GET /api/v1/tools lists the built-ins (widened reads) →
re-seeding is idempotent (unchanged) → POST /api/v1/tools registers a new tool under the caller's
own org (deterministic id) and it appears in that tenant's catalogue + capability match → a tenant's
custom tool is NOT visible to another tenant (writes stay strict caller-org). Key-free.

Reads run in ``gateway`` mode so each tenant supplies its own verified ``X-Organisation-Id`` while
trusting the gateway's ``X-Internal-Key`` (ADR-018). The platform org is distinct from any tenant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_PLATFORM_ORG = "00000000-0000-0000-0000-0000000000a0"
_TENANT_A = "00000000-0000-0000-0000-00000000aaaa"
_TENANT_B = "00000000-0000-0000-0000-00000000bbbb"
_PRINCIPAL = "00000000-0000-0000-0000-0000000000c5"
_INTERNAL_KEY = "dev-internal-key"


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", _INTERNAL_KEY)
    # gateway mode: each tenant supplies its own verified X-Organisation-Id (no fixed dev org).
    monkeypatch.setenv("AUTH_MODE", "gateway")
    monkeypatch.setenv("PLATFORM_ORG_ID", _PLATFORM_ORG)
    from oraclous_capability_registry_service.core.config import get_settings

    get_settings.cache_clear()

    from oraclous_capability_registry_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    import uuid as _uuid

    from oraclous_capability_registry_service.app.factory import create_app
    from oraclous_capability_registry_service.repositories.capability_repository import (
        CapabilityRepository,
    )
    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

    app = create_app(lifespan=None)
    # The repository is constructed with the platform org so tenant reads are widened to the
    # built-in catalogue (matching how core/lifespan wires it in production).
    repo = CapabilityRepository(async_dsn, platform_org_id=_uuid.UUID(_PLATFORM_ORG))
    app.state.capability_repository = repo
    # ASGITransport doesn't run the lifespan, so perform the startup plugin seed explicitly — under
    # the PLATFORM org, not any tenant org.
    app.state.seed_statuses = await sync_plugins(
        repository=repo, organisation_id=_uuid.UUID(_PLATFORM_ORG)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        c.seed_statuses = app.state.seed_statuses  # type: ignore[attr-defined]
        c.repo = repo  # type: ignore[attr-defined]
        yield c
    await repo.close()
    get_settings.cache_clear()


def _auth(org: str = _TENANT_A) -> dict:
    """Gateway-mode identity headers for a tenant org (ADR-018 edge-auth)."""
    return {
        "X-Internal-Key": _INTERNAL_KEY,
        "X-Principal-Id": _PRINCIPAL,
        "X-Principal-Type": "user",
        "X-Organisation-Id": org,
    }


async def test_startup_seeds_builtin_tools_visible_to_tenant(client: AsyncClient) -> None:
    # every plugin was created on the first (fixture) seed into the platform org
    assert all(s == "created" for s in client.seed_statuses.values())  # type: ignore[attr-defined]
    # a DIFFERENT tenant org sees the platform built-ins via the widened reads
    listed = await client.get("/api/v1/tools", headers=_auth(_TENANT_A))
    assert listed.status_code == 200
    out = listed.json()
    assert out["total"] >= 5
    names = {t["name"] for t in out["capabilities"]}
    assert {"PostgreSQL Reader", "MySQL Reader", "Google Drive Reader"} <= names
    # deterministic ids: the embedded descriptor id equals the row id
    for t in out["capabilities"]:
        assert t["descriptor"]["id"] == t["id"]


async def test_reseed_is_idempotent(client: AsyncClient) -> None:
    import uuid

    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

    repo = client.repo  # type: ignore[attr-defined]
    statuses = await sync_plugins(repository=repo, organisation_id=uuid.UUID(_PLATFORM_ORG))
    assert statuses and all(s == "unchanged" for s in statuses.values())
    listed = await client.get("/api/v1/tools", headers=_auth(_TENANT_A))
    before = listed.json()["total"]
    # a second resync does not duplicate rows
    await sync_plugins(repository=repo, organisation_id=uuid.UUID(_PLATFORM_ORG))
    again = (await client.get("/api/v1/tools", headers=_auth(_TENANT_A))).json()["total"]
    assert before == again


async def test_register_tool_is_deterministic_and_searchable(client: AsyncClient) -> None:
    desc = {
        "kind": "tool",
        "metadata": {"name": "Custom Echo Tool", "category": "UTILITY"},
        "version": {"semver": "1.0.0"},
        "spec": {
            "type": "INTERNAL",
            "capabilities": [{"name": "echo", "description": "echo input"}],
            "credential_requirements": [],
        },
    }
    r1 = await client.post("/api/v1/tools", json={"descriptor": desc}, headers=_auth(_TENANT_A))
    assert r1.status_code == 201, r1.text
    tid = r1.json()["id"]
    # re-registering the same tool yields the same deterministic id (idempotent)
    r2 = await client.post("/api/v1/tools", json={"descriptor": desc}, headers=_auth(_TENANT_A))
    assert r2.json()["id"] == tid

    got = await client.get(f"/api/v1/tools/{tid}", headers=_auth(_TENANT_A))
    assert got.status_code == 200 and got.json()["name"] == "Custom Echo Tool"

    matched = await client.post(
        "/api/v1/capabilities/match",
        json={"capabilities": ["echo"]},
        headers=_auth(_TENANT_A),
    )
    assert any(t["id"] == tid for t in matched.json()["capabilities"])


async def test_tenant_custom_tool_is_not_visible_to_other_tenant(client: AsyncClient) -> None:
    desc = {
        "kind": "tool",
        "metadata": {"name": "Tenant A Private Tool", "category": "UTILITY"},
        "version": {"semver": "1.0.0"},
        "spec": {
            "type": "INTERNAL",
            "capabilities": [{"name": "a-private-cap", "description": "private"}],
            "credential_requirements": [],
        },
    }
    created = await client.post(
        "/api/v1/tools", json={"descriptor": desc}, headers=_auth(_TENANT_A)
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]

    # Tenant A sees both the platform built-ins AND its own custom tool.
    a_list = (await client.get("/api/v1/tools", headers=_auth(_TENANT_A))).json()
    a_ids = {t["id"] for t in a_list["capabilities"]}
    a_names = {t["name"] for t in a_list["capabilities"]}
    assert tid in a_ids
    assert "PostgreSQL Reader" in a_names  # platform built-ins remain visible

    # Tenant B sees the platform built-ins but NOT tenant A's private tool.
    b_list = (await client.get("/api/v1/tools", headers=_auth(_TENANT_B))).json()
    b_ids = {t["id"] for t in b_list["capabilities"]}
    b_names = {t["name"] for t in b_list["capabilities"]}
    assert tid not in b_ids
    assert "Tenant A Private Tool" not in b_names
    assert "PostgreSQL Reader" in b_names  # but the global catalogue is shared

    # Direct get by id is also masked across tenants (404 for tenant B).
    got_b = await client.get(f"/api/v1/tools/{tid}", headers=_auth(_TENANT_B))
    assert got_b.status_code == 404


async def test_register_tool_without_name_is_422(client: AsyncClient) -> None:
    desc = {"kind": "tool", "metadata": {}, "spec": {"type": "INTERNAL", "capabilities": []}}
    resp = await client.post("/api/v1/tools", json={"descriptor": desc}, headers=_auth(_TENANT_A))
    assert resp.status_code == 422, resp.text
