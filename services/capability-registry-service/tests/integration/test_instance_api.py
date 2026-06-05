"""Integration: tool instances + execution-readiness validation vs real Postgres (S3).

Proves create-instance derives required credentials from the tool descriptor and sets
CONFIGURATION_REQUIRED → validate-execution reports the missing credential → configure-credentials
flips it to READY → validate is_ready → cross-org access is masked (404) → an instance for a
non-existent capability is rejected (404). Key-free (dev bearer + testcontainer Postgres).
"""

from __future__ import annotations

import uuid
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
    from oraclous_capability_registry_service.repositories.instance_repository import (
        InstanceRepository,
    )

    app = create_app(lifespan=None)
    repo = CapabilityRepository(async_dsn)
    inst_repo = InstanceRepository(async_dsn)
    app.state.capability_repository = repo
    app.state.instance_repository = inst_repo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        yield c
    await repo.close()
    await inst_repo.close()
    get_settings.cache_clear()


def _auth(bearer: str = "dev-token") -> dict:
    return {"Authorization": f"Bearer {bearer}"}


async def _register_oauth_tool(client: AsyncClient) -> str:
    desc = {
        "kind": "tool",
        "descriptor": {
            "kind": "tool",
            "metadata": {"name": "Drive Reader", "category": "INGESTION"},
            "spec": {
                "type": "API",
                "capabilities": [{"name": "read_drive_files", "description": "x"}],
                "credential_requirements": [
                    {"type": "oauth_token", "provider": "google", "scopes": ["drive.readonly"]}
                ],
            },
        },
    }
    created = await client.post("/api/v1/capabilities", json=desc, headers=_auth())
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_instance_lifecycle_configuration_required_then_ready(client: AsyncClient) -> None:
    cap_id = await _register_oauth_tool(client)

    created = await client.post(
        "/api/v1/instances",
        json={"capability_id": cap_id, "name": "my drive"},
        headers=_auth(),
    )
    assert created.status_code == 201, created.text
    inst = created.json()
    assert inst["status"] == "CONFIGURATION_REQUIRED"
    assert inst["required_credentials"] == ["oauth_token"]
    assert inst["organisation_id"] == _DEV_ORG
    iid = inst["id"]

    # validate-execution flags the missing credential
    report = (
        await client.get(f"/api/v1/instances/{iid}/validate-execution", headers=_auth())
    ).json()
    assert report["is_ready"] is False
    assert report["checks"]["credentials"] == "failed"
    assert any(a["credential_type"] == "oauth_token" for a in report["action_items"])

    # configure the credential mapping -> READY
    configured = await client.post(
        f"/api/v1/instances/{iid}/configure-credentials",
        json={"credential_mappings": {"oauth_token": "cred-123"}},
        headers=_auth(),
    )
    assert configured.status_code == 200
    assert configured.json()["status"] == "READY"

    # validate-execution now ready
    report2 = (
        await client.get(f"/api/v1/instances/{iid}/validate-execution", headers=_auth())
    ).json()
    assert report2["is_ready"] is True
    assert report2["checks"]["credentials"] == "passed"


async def test_instance_ready_when_no_credentials_required(client: AsyncClient) -> None:
    desc = {
        "kind": "tool",
        "descriptor": {
            "kind": "tool",
            "metadata": {"name": "Echo", "category": "UTILITY"},
            "spec": {
                "type": "INTERNAL",
                "capabilities": [{"name": "echo"}],
                "credential_requirements": [],
            },
        },
    }
    cap_id = (await client.post("/api/v1/capabilities", json=desc, headers=_auth())).json()["id"]
    created = await client.post(
        "/api/v1/instances", json={"capability_id": cap_id, "name": "e"}, headers=_auth()
    )
    assert created.json()["status"] == "READY"


async def test_instance_for_unknown_capability_is_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/instances",
        json={"capability_id": str(uuid.uuid4()), "name": "x"},
        headers=_auth(),
    )
    assert resp.status_code == 404


async def test_cross_org_instance_get_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap_id = await _register_oauth_tool(client)
    iid = (
        await client.post(
            "/api/v1/instances", json={"capability_id": cap_id, "name": "x"}, headers=_auth()
        )
    ).json()["id"]

    from oraclous_capability_registry_service.core.config import get_settings

    monkeypatch.setenv("DEV_ORG_ID", _OTHER_ORG)
    get_settings.cache_clear()
    try:
        resp = await client.get(f"/api/v1/instances/{iid}", headers=_auth())
        assert resp.status_code == 404
    finally:
        monkeypatch.setenv("DEV_ORG_ID", _DEV_ORG)
        get_settings.cache_clear()
