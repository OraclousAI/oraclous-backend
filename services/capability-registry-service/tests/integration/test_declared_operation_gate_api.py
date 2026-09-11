"""Security integration (#1004, item 1): the registry refuses an operation nothing declares.

Real service wiring — the real FastAPI app, the real seeded plugin catalogue, a real Postgres
(testcontainer), the real ``ToolExecutionService`` — with only the credential broker faked (it
mints a connection_string pointing at the test database, exactly as ``test_execution_api`` does).
No executor stands in for the refusal path: the whole point is that the call is refused BEFORE any
executor is created.

The threat (T3, model-returned tool-call dispatch): #956 closed the model→operation channel inside
the harness runtime. The registry itself still trusted the caller's ``operation``, so anything that
can reach ``POST /api/v1/instances/{id}/execute`` — the same route the engine's scheduled
adopted-tool worker drives — could ask a configured instance for any operation its connector class
happens to implement, not merely the ones the instance's descriptor declares. The two services now
agree independently.

RED until the #1004 ``[impl]`` lands: today ``PostgreSQL Reader`` answers an undeclared operation
with a 201 + a FAILED execution row, not a 409 refusal, and the provenance row is written anyway.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.tool_dispatch]

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"

#: The registry's coded refusal token — the closed lowercase vocabulary this endpoint already
#: speaks (``pending_approval`` / ``no_executor``), which the harness's registry client accepts
#: across the leak boundary and translates for the calling member (#692).
_CODE = "unsupported_operation"


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
    from oraclous_capability_registry_service.repositories.registry_provenance_sink import (
        PostgresProvenanceSink,
    )
    from oraclous_capability_registry_service.services.credential_client import (
        FakeCredentialBroker,
        _libpq_dsn,
    )
    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins
    from oraclous_substrate import ProvenanceCollector

    app = create_app(lifespan=None)
    repo = CapabilityRepository(async_dsn)
    inst_repo = InstanceRepository(async_dsn)
    exec_repo = ExecutionRepository(async_dsn)
    app.state.capability_repository = repo
    app.state.instance_repository = inst_repo
    app.state.execution_repository = exec_repo
    app.state.provenance = ProvenanceCollector(PostgresProvenanceSink(async_dsn))
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


async def _reader_instance(client: AsyncClient) -> str:
    """A ready ``PostgreSQL Reader`` instance — declared operations ``list_tables`` + ``query``."""
    tools = (await client.get("/api/v1/tools", headers=_auth())).json()["capabilities"]
    cap_id = next(t["id"] for t in tools if t["name"] == "PostgreSQL Reader")
    iid = (
        await client.post(
            "/api/v1/instances",
            json={"capability_id": cap_id, "name": "gate"},
            headers=_auth(),
        )
    ).json()["id"]
    await client.post(
        f"/api/v1/instances/{iid}/configure-credentials",
        json={"credential_mappings": {"connection_string": "cred-1"}},
        headers=_auth(),
    )
    return str(iid)


async def test_an_undeclared_operation_is_refused_with_a_coded_409(ctx: dict) -> None:
    client: AsyncClient = ctx["client"]
    iid = await _reader_instance(client)

    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "execute_ddl", "query": "DROP TABLE executions"}},
        headers=_auth(),
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == _CODE


async def test_the_refusal_writes_no_provenance_row_and_bumps_no_counter(ctx: dict) -> None:
    """Fail-closed BEFORE the executor means before the QUEUED provenance row too — a refused call
    is not an execution, and a caller must not be able to grow the executions table by guessing
    operation names."""
    client: AsyncClient = ctx["client"]
    iid = await _reader_instance(client)

    await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "execute_ddl"}},
        headers=_auth(),
    )

    inst = (await client.get(f"/api/v1/instances/{iid}", headers=_auth())).json()
    assert inst["execution_count"] == 0
    assert inst["last_execution_id"] is None


async def test_a_declared_operation_still_executes(ctx: dict) -> None:
    """The regression guard against over-refusing: ``list_tables`` IS declared by the plugin and
    must still run end-to-end against the real database."""
    client: AsyncClient = ctx["client"]
    iid = await _reader_instance(client)

    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "list_tables"}},
        headers=_auth(),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "SUCCESS"


async def test_a_call_with_no_operation_is_not_refused(ctx: dict) -> None:
    """Shipped callers omit the key and rely on the connector's own default (here ``query``). The
    gate must fire on a caller CHOOSING an undeclared operation, never on choosing nothing."""
    client: AsyncClient = ctx["client"]
    iid = await _reader_instance(client)

    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"query": "SELECT 1 AS n"}},
        headers=_auth(),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["output_data"]["rows"] == [{"n": 1}]


async def test_the_409_body_never_echoes_the_supplied_operation(ctx: dict) -> None:
    """The 409 body is read by the harness and turned into words a MODEL reads. An unbounded echo
    of the caller's own string would make the refusal a general relay channel — the class #956
    closed on the harness side and #697 bounded for a tool's own words."""
    client: AsyncClient = ctx["client"]
    iid = await _reader_instance(client)
    injected = "q" * 4000

    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": injected}},
        headers=_auth(),
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == _CODE
    assert injected not in resp.text
    from oraclous_capability_registry_service.domain.executors.base import TOOL_ERROR_CHARS

    assert len(resp.text) <= 2 * TOOL_ERROR_CHARS, "the refusal body is not bounded"
