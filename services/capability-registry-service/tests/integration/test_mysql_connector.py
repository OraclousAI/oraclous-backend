"""Integration: the MySQL connector executes a real query (S5b) — relational breadth proof.

A MySQL Reader instance runs ``list_tables`` and a parameterized ``query`` against a real MySQL
container (the FakeCredentialBroker mints the MySQL connection_string), proving the second
relational connector is real. The app's own store is Postgres; MySQL is the external data source.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import aiomysql
import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"


async def _seed_mysql(mysql_dsn: str) -> None:
    p = urlparse(mysql_dsn)
    conn = await aiomysql.connect(
        host=p.hostname,
        port=p.port or 3306,
        user=p.username,
        password=p.password or "",
        db=p.path.lstrip("/"),
    )
    try:
        async with conn.cursor() as cur:
            # idempotent across tests (the MySQL container is session-scoped)
            await cur.execute("DROP TABLE IF EXISTS widgets")
            await cur.execute("CREATE TABLE widgets (id INT PRIMARY KEY, name TEXT)")
            await cur.execute("INSERT INTO widgets (id, name) VALUES (1, 'alpha')")
            await conn.commit()
    finally:
        conn.close()


@pytest.fixture
async def client(
    postgres_dsn: str, mysql_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    await _seed_mysql(mysql_dsn)
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
    from oraclous_capability_registry_service.repositories.execution_repository import (
        ExecutionRepository,
    )
    from oraclous_capability_registry_service.repositories.instance_repository import (
        InstanceRepository,
    )
    from oraclous_capability_registry_service.services.credential_client import FakeCredentialBroker
    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

    app = create_app(lifespan=None)
    repo = CapabilityRepository(async_dsn)
    app.state.capability_repository = repo
    app.state.instance_repository = InstanceRepository(async_dsn)
    app.state.execution_repository = ExecutionRepository(async_dsn)
    # provider-keyed fake broker: a connection_string for "mysql" resolves to the MySQL container.
    app.state.credential_broker = FakeCredentialBroker(
        fake_db_dsn="unused", dsn_by_provider={"mysql": mysql_dsn}
    )
    await sync_plugins(repository=repo, organisation_id=uuid.UUID(_DEV_ORG))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        c.repos = (repo, app.state.instance_repository, app.state.execution_repository)  # type: ignore[attr-defined]
        yield c
    for r in c.repos:  # type: ignore[attr-defined]
        await r.close()
    get_settings.cache_clear()


def _auth() -> dict:
    return {"Authorization": "Bearer dev-token"}


async def _mysql_instance(client: AsyncClient) -> str:
    tools = (await client.get("/api/v1/tools", headers=_auth())).json()["capabilities"]
    cap_id = next(t["id"] for t in tools if t["name"] == "MySQL Reader")
    iid = (
        await client.post(
            "/api/v1/instances", json={"capability_id": cap_id, "name": "my"}, headers=_auth()
        )
    ).json()["id"]
    await client.post(
        f"/api/v1/instances/{iid}/configure-credentials",
        json={"credential_mappings": {"connection_string": "cred-1"}},
        headers=_auth(),
    )
    return iid


async def test_mysql_list_tables_returns_real_rows(client: AsyncClient) -> None:
    iid = await _mysql_instance(client)
    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "list_tables"}},
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["status"] == "SUCCESS"
    assert "widgets" in out["output_data"]["tables"]


async def test_mysql_parameterized_query(client: AsyncClient) -> None:
    iid = await _mysql_instance(client)
    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={
            "input_data": {
                "operation": "query",
                "query": "SELECT name FROM widgets WHERE id = %s",
                "parameters": [1],
            }
        },
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["status"] == "SUCCESS"
    assert out["output_data"]["rows"] == [{"name": "alpha"}]
