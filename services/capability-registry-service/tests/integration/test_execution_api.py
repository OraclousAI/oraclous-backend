"""Integration: synchronous tool execution vs real Postgres (S4) — the §22 not-hollow proof.

With the FakeCredentialBroker minting a connection_string to the test database, a PostgreSQL Reader
instance executes ``list_tables`` end-to-end → real rows are returned, an ``executions`` provenance
row is written (SUCCESS) with ``credential_refs`` populated and the secret ABSENT, and the instance
counters are bumped. A tool with no executor returns 409. Key-free (fake broker + testcontainer PG).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"


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
    # Fake broker mints a connection_string pointing at THIS test database → real query, key-free.
    app.state.credential_broker = FakeCredentialBroker(fake_db_dsn=_libpq_dsn(async_dsn))
    await sync_plugins(repository=repo, organisation_id=uuid.UUID(_DEV_ORG))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        yield {"client": c, "exec_repo": exec_repo}
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


async def test_postgres_reader_executes_real_query(ctx: dict) -> None:
    client: AsyncClient = ctx["client"]
    cap_id = await _tool_id(client, "PostgreSQL Reader")
    iid = await _ready_instance(client, cap_id, "connection_string")

    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "list_tables"}},
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["status"] == "SUCCESS"
    tables = out["output_data"]["tables"]
    # the schema this service created lives in the same DB → real rows come back
    assert "capability_descriptors" in tables
    assert "tool_instances" in tables

    # provenance: credential_refs record the type/provider used, never the secret material
    refs = out["credential_refs"]
    assert refs and refs[0]["type"] == "connection_string"
    assert "fake_db_dsn" not in resp.text
    assert 'connection_string":"postgresql' not in resp.text  # the DSN secret is not echoed

    # the execution row is persisted and fetchable; output holds no secret
    exec_id = out["id"]
    got = await client.get(f"/api/v1/executions/{exec_id}", headers=_auth())
    assert got.status_code == 200 and got.json()["status"] == "SUCCESS"

    # instance counters were bumped
    inst = (await client.get(f"/api/v1/instances/{iid}", headers=_auth())).json()
    assert inst["execution_count"] == 1
    assert inst["last_execution_id"] == exec_id


async def test_parameterized_query_returns_rows(ctx: dict) -> None:
    client: AsyncClient = ctx["client"]
    cap_id = await _tool_id(client, "PostgreSQL Reader")
    iid = await _ready_instance(client, cap_id, "connection_string")
    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={
            "input_data": {"operation": "query", "query": "SELECT $1::int AS n", "parameters": [7]}
        },
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["status"] == "SUCCESS"
    assert out["output_data"]["rows"] == [{"n": 7}]


async def test_tool_without_executor_is_409(ctx: dict) -> None:
    client: AsyncClient = ctx["client"]
    # Notion Reader is registered (S2) but has no executor in R3.5 → not executable.
    cap_id = await _tool_id(client, "Notion Reader")
    iid = await _ready_instance(client, cap_id, "api_key")
    resp = await client.post(
        f"/api/v1/instances/{iid}/execute", json={"input_data": {}}, headers=_auth()
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "no_executor"


async def test_secret_absent_from_executions_row(ctx: dict) -> None:
    client: AsyncClient = ctx["client"]
    exec_repo = ctx["exec_repo"]
    cap_id = await _tool_id(client, "PostgreSQL Reader")
    iid = await _ready_instance(client, cap_id, "connection_string")
    out = (
        await client.post(
            f"/api/v1/instances/{iid}/execute",
            json={"input_data": {"operation": "list_tables"}},
            headers=_auth(),
        )
    ).json()
    row = await exec_repo.get_by_id(uuid.UUID(out["id"]), uuid.UUID(_DEV_ORG))
    # the stored credential_refs carry only type/provider/scopes — never the resolved secret
    assert row is not None
    ref = row.credential_refs[0]
    assert ref["type"] == "connection_string"
    assert set(ref.keys()) == {"type", "provider", "scopes"}
    assert "postgresql://" not in str(row.credential_refs)
