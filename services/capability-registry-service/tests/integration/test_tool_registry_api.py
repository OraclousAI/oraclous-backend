"""Integration: tool registry + plugin seeding vs real Postgres (S2).

Proves startup plugin discovery seeds the built-in tool catalogue (deterministic ids) → GET
/api/v1/tools lists them → re-seeding is idempotent (unchanged) → POST /api/v1/tools registers a new
tool with a deterministic id and it appears in the catalogue + capability match. Key-free.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"


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

    from oraclous_capability_registry_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from oraclous_capability_registry_service.app.factory import create_app
    from oraclous_capability_registry_service.repositories.capability_repository import (
        CapabilityRepository,
    )

    app = create_app(lifespan=None)
    repo = CapabilityRepository(async_dsn)
    app.state.capability_repository = repo
    # ASGITransport doesn't run the lifespan, so perform the startup plugin seed explicitly.
    import uuid as _uuid

    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

    app.state.seed_statuses = await sync_plugins(
        repository=repo, organisation_id=_uuid.UUID(_DEV_ORG)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        c.seed_statuses = app.state.seed_statuses  # type: ignore[attr-defined]
        c.repo = repo  # type: ignore[attr-defined]
        yield c
    await repo.close()
    get_settings.cache_clear()


def _auth(bearer: str = "dev-token") -> dict:
    return {"Authorization": f"Bearer {bearer}"}


async def test_startup_seeds_builtin_tools(client: AsyncClient) -> None:
    # every plugin was created on the first (fixture) seed
    assert all(s == "created" for s in client.seed_statuses.values())  # type: ignore[attr-defined]
    listed = await client.get("/api/v1/tools", headers=_auth())
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
    statuses = await sync_plugins(repository=repo, organisation_id=uuid.UUID(_DEV_ORG))
    assert statuses and all(s == "unchanged" for s in statuses.values())
    listed = await client.get("/api/v1/tools", headers=_auth())
    before = listed.json()["total"]
    # a second resync does not duplicate rows
    await sync_plugins(repository=repo, organisation_id=uuid.UUID(_DEV_ORG))
    again = (await client.get("/api/v1/tools", headers=_auth())).json()["total"]
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
    r1 = await client.post("/api/v1/tools", json={"descriptor": desc}, headers=_auth())
    assert r1.status_code == 201, r1.text
    tid = r1.json()["id"]
    # re-registering the same tool yields the same deterministic id (idempotent)
    r2 = await client.post("/api/v1/tools", json={"descriptor": desc}, headers=_auth())
    assert r2.json()["id"] == tid

    got = await client.get(f"/api/v1/tools/{tid}", headers=_auth())
    assert got.status_code == 200 and got.json()["name"] == "Custom Echo Tool"

    matched = await client.post(
        "/api/v1/capabilities/match", json={"capabilities": ["echo"]}, headers=_auth()
    )
    assert any(t["id"] == tid for t in matched.json()["capabilities"])


async def test_register_tool_without_name_is_422(client: AsyncClient) -> None:
    desc = {"kind": "tool", "metadata": {}, "spec": {"type": "INTERNAL", "capabilities": []}}
    resp = await client.post("/api/v1/tools", json={"descriptor": desc}, headers=_auth())
    assert resp.status_code == 422, resp.text
