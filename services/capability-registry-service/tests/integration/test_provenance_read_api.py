"""Integration: the registry's own provenance read path vs real Postgres (#826, 11 Sep ruling).

A write-only emit is not provable (RULE 5 forbids a DB-direct assertion in an e2e, and the registry
exposed no read surface at all), so the ruling adds ``registry_provenance`` + a read-only repository
+ ``GET /api/v1/provenance`` alongside the ``execute_sync`` collector emit. This drives a REAL
dispatch through the app and reads it back through the new HTTP endpoint — never DB-direct. Mirrors
``test_execution_api.py``'s ``ctx`` fixture wiring; key-free (fake broker + testcontainer PG).

RED until the ``registry_provenance`` table/migration, the read-only repository, and
``GET /api/v1/provenance`` all exist — the endpoint 404s today.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_DEV_ORG = "00000000-0000-0000-0000-00000000050b"

_RULED_FIELDS = {
    "id",
    "action",
    "resource",
    "outcome",
    "created_at",
    "principal",
    "context",
    "input_hash",
    "output_hash",
}


@pytest.fixture
async def ctx(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DEV_BEARER", "dev-token")
    monkeypatch.setenv("DEV_ORG_ID", _DEV_ORG)
    monkeypatch.setenv("CREDENTIAL_BROKER_MODE", "fake")
    from oraclous_capability_registry_service.core.config import get_settings

    get_settings.cache_clear()

    from oraclous_capability_registry_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        # Once the impl lands, the new RegistryProvenance ORM model registers on this same shared
        # Base metadata, so this create_all picks up `registry_provenance` with no change here.
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from oraclous_capability_registry_service.app.factory import create_app
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
    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

    app = create_app(lifespan=None)
    repo = CapabilityRepository(async_dsn)
    inst_repo = InstanceRepository(async_dsn)
    exec_repo = ExecutionRepository(async_dsn)
    app.state.capability_repository = repo
    app.state.instance_repository = inst_repo
    app.state.execution_repository = exec_repo
    app.state.credential_broker = FakeCredentialBroker(fake_db_dsn=_libpq_dsn(async_dsn))
    await sync_plugins(repository=repo, organisation_id=uuid.UUID(_DEV_ORG))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        yield {"client": c}
    await repo.close()
    await inst_repo.close()
    await exec_repo.close()
    get_settings.cache_clear()


def _auth() -> dict:
    return {"Authorization": "Bearer dev-token"}


async def _tool_id(client: AsyncClient, name: str) -> str:
    tools = (await client.get("/api/v1/tools", headers=_auth())).json()["capabilities"]
    return next(t["id"] for t in tools if t["name"] == name)


async def _ready_instance(client: AsyncClient, cap_id: str, cred_type: str) -> str:
    iid = (
        await client.post(
            "/api/v1/instances", json={"capability_id": cap_id, "name": "x"}, headers=_auth()
        )
    ).json()["id"]
    await client.post(
        f"/api/v1/instances/{iid}/configure-credentials",
        json={"credential_mappings": {cred_type: "cred-1"}},
        headers=_auth(),
    )
    return iid


_INPUT_DATA = {"operation": "list_tables"}


async def _dispatch(client: AsyncClient, iid: str) -> dict:
    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": _INPUT_DATA},
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_a_dispatch_is_readable_through_the_provenance_endpoint(ctx: dict) -> None:
    client: AsyncClient = ctx["client"]
    cap_id = await _tool_id(client, "PostgreSQL Reader")
    iid = await _ready_instance(client, cap_id, "connection_string")
    executed = await _dispatch(client, iid)

    resp = await client.get("/api/v1/provenance", headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    event = body["events"][0]
    assert set(event) == _RULED_FIELDS
    assert event["action"] == "capability.invoke"
    assert event["resource"] == f"tool_instance:{iid}"
    assert event["outcome"] == "succeeded"
    assert event["created_at"] is not None
    assert event["principal"]

    from oraclous_substrate.provenance import hash_payload  # function-local, per the seam rule

    # Pinned against the SAME input_data this test POSTed, and the SAME output_data the execute
    # response actually returned — never a hand-written hex string, and never merely "is not None".
    assert event["input_hash"] == hash_payload(_INPUT_DATA)
    assert event["output_hash"] == hash_payload(executed["output_data"])


async def test_events_are_newest_first(ctx: dict) -> None:
    client: AsyncClient = ctx["client"]
    cap_id = await _tool_id(client, "PostgreSQL Reader")
    iid = await _ready_instance(client, cap_id, "connection_string")
    for _ in range(3):
        await _dispatch(client, iid)

    resp = await client.get("/api/v1/provenance", headers=_auth())
    assert resp.status_code == 200, resp.text
    created = [e["created_at"] for e in resp.json()["events"]]
    assert len(created) >= 3
    assert created == sorted(created, reverse=True)


async def test_limit_caps_the_result(ctx: dict) -> None:
    client: AsyncClient = ctx["client"]
    cap_id = await _tool_id(client, "PostgreSQL Reader")
    iid = await _ready_instance(client, cap_id, "connection_string")
    for _ in range(3):
        await _dispatch(client, iid)

    resp = await client.get("/api/v1/provenance", params={"limit": 2}, headers=_auth())
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["events"]) == 2


async def test_out_of_range_limit_is_rejected(ctx: dict) -> None:
    # mirrors GET /v1/engine/activity's `Query(ge=1, le=MAX_ACTIVITY_LIMIT)` bound.
    client: AsyncClient = ctx["client"]
    resp = await client.get("/api/v1/provenance", params={"limit": 0}, headers=_auth())
    assert resp.status_code == 422
