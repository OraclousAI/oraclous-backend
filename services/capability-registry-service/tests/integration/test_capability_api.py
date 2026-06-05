"""Integration: capability registry CRUD + search/match vs real Postgres (S1).

Proves register → GET returns the descriptor with a computed content_hash → list/match by
capability name → cross-org read is denied (404, mask) → malformed oauth-without-scopes is rejected
(422) → update recomputes the hash → delete. Dev-auth seam binds the org from the bearer (ORG001).
Key-free (dev bearer + testcontainer Postgres).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"
_OTHER_ORG = "00000000-0000-0000-0000-0000000006ff"


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DEV_BEARER", "dev-token")
    monkeypatch.setenv("DEV_ORG_ID", _DEV_ORG)
    from oraclous_capability_registry_service.core.config import get_settings

    get_settings.cache_clear()

    # Create the schema (the Alembic one-shot does this in docker; here we do it directly).
    from oraclous_capability_registry_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    # ASGITransport doesn't run the lifespan, so wire app.state directly (mirrors sibling tests).
    from oraclous_capability_registry_service.app.factory import create_app
    from oraclous_capability_registry_service.repositories.capability_repository import (
        CapabilityRepository,
    )

    app = create_app(lifespan=None)
    repo = CapabilityRepository(async_dsn)
    app.state.capability_repository = repo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        yield c
    await repo.close()
    get_settings.cache_clear()


def _auth(bearer: str = "dev-token") -> dict:
    return {"Authorization": f"Bearer {bearer}"}


def _tool_descriptor(name: str = "Google Drive Reader", cap: str = "read_drive_files") -> dict:
    return {
        "kind": "tool",
        "descriptor": {
            "kind": "tool",
            "metadata": {"name": name, "category": "INGESTION"},
            "spec": {
                "type": "INTERNAL",
                "capabilities": [{"name": cap, "description": "..."}],
                "credential_requirements": [
                    {"type": "oauth_token", "provider": "google", "scopes": ["drive.readonly"]}
                ],
            },
        },
    }


async def test_register_then_get_with_computed_hash(client: AsyncClient) -> None:
    created = await client.post("/api/v1/capabilities", json=_tool_descriptor(), headers=_auth())
    assert created.status_code == 201, created.text
    out = created.json()
    assert out["organisation_id"] == _DEV_ORG
    assert out["name"] == "Google Drive Reader"
    assert out["content_hash"] and len(out["content_hash"]) == 64

    got = await client.get(f"/api/v1/capabilities/{out['id']}", headers=_auth())
    assert got.status_code == 200
    assert got.json()["content_hash"] == out["content_hash"]


async def test_list_and_match_by_capability(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/capabilities",
        json=_tool_descriptor(name="A", cap="read_drive_files"),
        headers=_auth(),
    )
    await client.post(
        "/api/v1/capabilities",
        json=_tool_descriptor(name="B", cap="list_repos"),
        headers=_auth(),
    )

    listed = await client.get("/api/v1/capabilities", headers=_auth())
    assert listed.status_code == 200
    assert listed.json()["total"] == 2

    matched = await client.post(
        "/api/v1/capabilities/match",
        json={"capabilities": ["read_drive_files"]},
        headers=_auth(),
    )
    assert matched.status_code == 200
    rows = matched.json()["capabilities"]
    assert len(rows) == 1 and rows[0]["name"] == "A"


async def test_cross_org_get_is_404(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    created = await client.post("/api/v1/capabilities", json=_tool_descriptor(), headers=_auth())
    cap_id = created.json()["id"]

    # Re-point the dev-auth seam at a different org id and re-read the same descriptor id.
    from oraclous_capability_registry_service.core.config import get_settings

    monkeypatch.setenv("DEV_ORG_ID", _OTHER_ORG)
    get_settings.cache_clear()
    try:
        other = await client.get(f"/api/v1/capabilities/{cap_id}", headers=_auth())
        assert other.status_code == 404
    finally:
        monkeypatch.setenv("DEV_ORG_ID", _DEV_ORG)
        get_settings.cache_clear()


async def test_malformed_oauth_descriptor_is_422(client: AsyncClient) -> None:
    bad = _tool_descriptor()
    bad["descriptor"]["spec"]["credential_requirements"][0]["scopes"] = []
    resp = await client.post("/api/v1/capabilities", json=bad, headers=_auth())
    assert resp.status_code == 422, resp.text


async def test_update_recomputes_hash_and_delete(client: AsyncClient) -> None:
    created = await client.post("/api/v1/capabilities", json=_tool_descriptor(), headers=_auth())
    cap_id = created.json()["id"]
    original_hash = created.json()["content_hash"]

    new_desc = _tool_descriptor(name="Renamed")["descriptor"]
    updated = await client.put(
        f"/api/v1/capabilities/{cap_id}", json={"descriptor": new_desc}, headers=_auth()
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed"
    assert updated.json()["content_hash"] != original_hash

    deleted = await client.delete(f"/api/v1/capabilities/{cap_id}", headers=_auth())
    assert deleted.status_code == 204
    gone = await client.get(f"/api/v1/capabilities/{cap_id}", headers=_auth())
    assert gone.status_code == 404


async def test_missing_bearer_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/capabilities")
    assert resp.status_code == 401
