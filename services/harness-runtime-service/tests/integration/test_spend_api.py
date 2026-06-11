"""Integration: GET /v1/harnesses/spend vs real Postgres (#252; ORAA-4 §22).

Seeds ``harness_executions`` for two orgs across two models (one priced, one unknown), then proves
the real SQL aggregation + read-time pricing:
  * per-model raw token sums + execution count are correct;
  * the priced model carries estimated_usd (the per-Mtok math), the unknown reports tokens only;
  * a second org's executions are EXCLUDED (org-scoping);
  * totals sum tokens over every model but USD only over priced rows.

Per ADR-009 the rows store RAW tokens; the USD is computed at read time and is labelled an ESTIMATE
of the user's provider spend (BYOM), never platform billing.

Key-free: dev bearer (HARNESS_AUTH_MODE=dev) + a testcontainer Postgres; org-scoping is exercised by
flipping HARNESS_DEV_ORG_ID between the two seeded tenants.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"
_OTHER_ORG = "00000000-0000-0000-0000-0000000006ff"
_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# dev org: two executions on a priced model + one on an unknown model.
_PRICED_MODEL = "openrouter/openai/gpt-4o-mini"  # 0.15/Mtok in, 0.60/Mtok out
_UNKNOWN_MODEL = "openrouter/acme/mystery-7b"


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("HARNESS_DATABASE_URL", async_dsn)
    monkeypatch.setenv("HARNESS_AUTH_MODE", "dev")
    monkeypatch.setenv("HARNESS_DEV_BEARER", "dev-token")
    monkeypatch.setenv("HARNESS_DEV_ORG_ID", _DEV_ORG)

    from oraclous_harness_runtime_service.core.config import get_settings

    get_settings.cache_clear()

    from oraclous_harness_runtime_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    await _seed(async_dsn)

    from oraclous_harness_runtime_service.app.factory import create_app
    from oraclous_harness_runtime_service.repositories.execution_repository import (
        ExecutionRepository,
    )

    app = create_app()
    repo = ExecutionRepository(async_dsn)
    app.state.execution_repository = repo
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://harness.test") as c:
        yield c
    await repo.close()
    get_settings.cache_clear()


async def _seed(async_dsn: str) -> None:
    from oraclous_harness_runtime_service.models.execution import HarnessExecution
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    def _row(org: str, model: str | None, inp: int, out: int, minute: int) -> HarnessExecution:
        return HarnessExecution(
            id=uuid.uuid4(),
            organisation_id=uuid.UUID(org),
            user_id=uuid.uuid4(),
            harness_id=uuid.uuid4(),
            harness_name="Demo",
            content_hash=None,
            status="SUCCEEDED",
            input="go",
            output="done",
            error_type=None,
            error_message=None,
            iterations=1,
            total_tokens=inp + out,
            model=model,
            input_tokens=inp,
            output_tokens=out,
            steps=[],
            created_at=_BASE + timedelta(minutes=minute),
        )

    engine = create_async_engine(async_dsn)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            # dev org — two priced-model runs (sum to 1M in / 1M out) + one unknown-model run.
            session.add(_row(_DEV_ORG, _PRICED_MODEL, 600_000, 600_000, 0))
            session.add(_row(_DEV_ORG, _PRICED_MODEL, 400_000, 400_000, 1))
            session.add(_row(_DEV_ORG, _UNKNOWN_MODEL, 2_000, 500, 2))
            # other org — a big gpt-4o run that must NEVER appear in the dev org's spend.
            session.add(_row(_OTHER_ORG, "openrouter/openai/gpt-4o", 5_000_000, 9_000_000, 0))
    await engine.dispose()


def _auth(bearer: str = "dev-token") -> dict:
    return {"Authorization": f"Bearer {bearer}"}


async def test_spend_prices_known_excludes_other_org(client: AsyncClient) -> None:
    resp = await client.get("/v1/harnesses/spend", headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["currency"] == "USD"
    by_model = {row["model"]: row for row in body["by_model"]}

    # priced model: the two runs aggregate to 1M in + 1M out → 0.15 + 0.60 = 0.75 USD.
    priced = by_model[_PRICED_MODEL]
    assert priced["input_tokens"] == 1_000_000
    assert priced["output_tokens"] == 1_000_000
    assert priced["executions"] == 2
    assert priced["priced"] is True
    assert priced["estimated_usd"] == pytest.approx(0.75)

    # unknown model: tokens only, no fabricated price.
    unknown = by_model[_UNKNOWN_MODEL]
    assert unknown["priced"] is False
    assert unknown["estimated_usd"] is None
    assert unknown["input_tokens"] == 2_000 and unknown["output_tokens"] == 500

    # the other org's gpt-4o run is excluded entirely (org-scoping).
    assert "openrouter/openai/gpt-4o" not in by_model

    # totals: USD over priced rows only; tokens over every row.
    assert body["total_estimated_usd"] == pytest.approx(0.75)
    assert body["total_input_tokens"] == 1_002_000
    assert body["total_output_tokens"] == 1_000_500
    assert body["unpriced_models"] == [_UNKNOWN_MODEL]


async def test_spend_is_org_scoped_to_other_org(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oraclous_harness_runtime_service.core.config import get_settings

    monkeypatch.setenv("HARNESS_DEV_ORG_ID", _OTHER_ORG)
    get_settings.cache_clear()
    try:
        body = (await client.get("/v1/harnesses/spend", headers=_auth())).json()
    finally:
        monkeypatch.setenv("HARNESS_DEV_ORG_ID", _DEV_ORG)
        get_settings.cache_clear()
    # the other org sees ONLY its own gpt-4o run — never the dev org's models.
    assert [row["model"] for row in body["by_model"]] == ["openrouter/openai/gpt-4o"]
    # gpt-4o = 2.50/Mtok in, 10.00/Mtok out → 5M*2.50/1e6 + 9M*10/1e6 = 12.50 + 90.00 = 102.50.
    assert body["total_estimated_usd"] == pytest.approx(102.50)


async def test_spend_since_window_lower_bounds(client: AsyncClient) -> None:
    # only the unknown-model run is at minute 2; a `since` after the priced runs drops them.
    since = (_BASE + timedelta(minutes=2)).isoformat()
    body = (
        await client.get("/v1/harnesses/spend", params={"since": since}, headers=_auth())
    ).json()
    assert [row["model"] for row in body["by_model"]] == [_UNKNOWN_MODEL]
    assert body["total_estimated_usd"] == pytest.approx(0.0)
    assert body["total_input_tokens"] == 2_000
