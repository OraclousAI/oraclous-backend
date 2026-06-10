"""Integration: GET /v1/engine/activity + /v1/engine/usage vs real Postgres (ORAA-4 §22).

Seeds ``engine_provenance`` rows for two orgs, then proves:
  * /activity returns ONLY the caller's events, newest-first, and honours ``limit`` (default + cap);
  * /usage returns the caller's RAW per-action counts only (cross-org isolation), and the ``since``
    window lower-bounds the counts. Per ADR-009 /usage is a count signal — never a price.

Key-free: dev bearer (ENGINE_AUTH_MODE=dev) + a testcontainer Postgres. Org-scoping is exercised by
flipping ENGINE_DEV_ORG_ID between the two seeded tenants.
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


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("ENGINE_DATABASE_URL", async_dsn)
    monkeypatch.setenv("ENGINE_AUTH_MODE", "dev")
    monkeypatch.setenv("ENGINE_DEV_BEARER", "dev-token")
    monkeypatch.setenv("ENGINE_DEV_ORG_ID", _DEV_ORG)

    from oraclous_execution_engine_service.core.config import get_settings

    get_settings.cache_clear()

    from oraclous_execution_engine_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    # seed provenance rows for two orgs (see _seed) BEFORE the app opens its own engine.
    await _seed(async_dsn)

    from oraclous_execution_engine_service.app.factory import create_app
    from oraclous_execution_engine_service.repositories.provenance_repository import (
        ProvenanceRepository,
    )

    app = create_app()
    repo = ProvenanceRepository(async_dsn)
    # set state directly so the suite does not depend on the lifespan Postgres handshake.
    app.state.provenance_repository = repo
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://engine.test") as c:
        yield c
    await repo.close()
    get_settings.cache_clear()


# Fixed seed: the dev org gets 5 events across 3 actions over time; the other org gets 2 events of a
# distinct action. The newest dev-org event is `job.cancel` so newest-first assertions are exact.
_DEV_ACTIONS = [
    "engine.job.submit",
    "engine.job.submit",
    "engine.job.submit",
    "engine.job.run",
    "engine.job.cancel",
]
_OTHER_ACTIONS = ["engine.schedule.fire", "engine.schedule.fire"]
_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


async def _seed(async_dsn: str) -> None:
    from oraclous_execution_engine_service.models.provenance import EngineProvenanceEvent
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(async_dsn)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            for i, action in enumerate(_DEV_ACTIONS):
                session.add(
                    EngineProvenanceEvent(
                        id=uuid.uuid4(),
                        organisation_id=uuid.UUID(_DEV_ORG),
                        principal="user-1",
                        action=action,
                        resource=f"engine_job:{uuid.uuid4()}",
                        outcome="SUCCEEDED",
                        created_at=_BASE + timedelta(minutes=i),  # ascending; last = newest
                    )
                )
            for action in _OTHER_ACTIONS:
                session.add(
                    EngineProvenanceEvent(
                        id=uuid.uuid4(),
                        organisation_id=uuid.UUID(_OTHER_ORG),
                        principal="user-2",
                        action=action,
                        resource=f"engine_job:{uuid.uuid4()}",
                        outcome="SUCCEEDED",
                        created_at=_BASE + timedelta(minutes=i),
                    )
                )
    await engine.dispose()


def _auth(bearer: str = "dev-token") -> dict:
    return {"Authorization": f"Bearer {bearer}"}


# ── /activity ────────────────────────────────────────────────────────────────────────────────────
async def test_activity_returns_only_callers_rows_newest_first(client: AsyncClient) -> None:
    resp = await client.get("/v1/engine/activity", headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == len(_DEV_ACTIONS)  # exactly the dev org's 5 — none of the other org's
    actions = [e["action"] for e in body["events"]]
    # newest-first: the seed's last dev event (job.cancel) is first, the first (job.submit) is last.
    assert actions[0] == "engine.job.cancel"
    assert actions[-1] == "engine.job.submit"
    # never another tenant's event:
    assert all(a != "engine.schedule.fire" for a in actions)
    # the DTO is the read projection (no organisation_id/principal leaked into the feed body).
    first = body["events"][0]
    assert set(first) == {"id", "action", "resource", "outcome", "created_at"}


async def test_activity_honours_limit(client: AsyncClient) -> None:
    resp = await client.get("/v1/engine/activity?limit=2", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    # the 2 newest dev-org events, newest-first.
    assert [e["action"] for e in body["events"]] == ["engine.job.cancel", "engine.job.run"]


async def test_activity_limit_is_capped(client: AsyncClient) -> None:
    # over the cap (200) is rejected by the query validator — the client can never drain the table.
    resp = await client.get("/v1/engine/activity?limit=5000", headers=_auth())
    assert resp.status_code == 422


async def test_activity_is_org_scoped(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from oraclous_execution_engine_service.core.config import get_settings

    monkeypatch.setenv("ENGINE_DEV_ORG_ID", _OTHER_ORG)
    get_settings.cache_clear()
    try:
        body = (await client.get("/v1/engine/activity", headers=_auth())).json()
    finally:
        monkeypatch.setenv("ENGINE_DEV_ORG_ID", _DEV_ORG)
        get_settings.cache_clear()
    # the other org sees ONLY its own 2 schedule.fire events — never the dev org's.
    assert body["total"] == len(_OTHER_ACTIONS)
    assert {e["action"] for e in body["events"]} == {"engine.schedule.fire"}


# ── /usage ────────────────────────────────────────────────────────────────────────────────────────
async def test_usage_returns_per_action_counts_for_caller_only(client: AsyncClient) -> None:
    resp = await client.get("/v1/engine/usage", headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    counts = {row["action"]: row["count"] for row in body["usage"]}
    assert counts == {"engine.job.submit": 3, "engine.job.run": 1, "engine.job.cancel": 1}
    assert body["total_events"] == 5
    # cross-org isolation: the other org's action never appears in this org's usage.
    assert "engine.schedule.fire" not in counts
    # RAW signal only (ADR-009): no price/USD/credits field on the count row.
    assert set(body["usage"][0]) == {"action", "count"}


async def test_usage_is_org_scoped(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from oraclous_execution_engine_service.core.config import get_settings

    monkeypatch.setenv("ENGINE_DEV_ORG_ID", _OTHER_ORG)
    get_settings.cache_clear()
    try:
        body = (await client.get("/v1/engine/usage", headers=_auth())).json()
    finally:
        monkeypatch.setenv("ENGINE_DEV_ORG_ID", _DEV_ORG)
        get_settings.cache_clear()
    counts = {row["action"]: row["count"] for row in body["usage"]}
    assert counts == {"engine.schedule.fire": 2}  # only the other org's own events
    assert body["total_events"] == 2


async def test_usage_since_window_lower_bounds_counts(client: AsyncClient) -> None:
    # the dev org's events are at _BASE + 0..4 min; a `since` of +3 min keeps only the last two.
    # pass via `params=` so httpx URL-encodes the `+00:00` offset (a raw `+` decodes to a space).
    since = (_BASE + timedelta(minutes=3)).isoformat()
    resp = await client.get("/v1/engine/usage", params={"since": since}, headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    counts = {row["action"]: row["count"] for row in body["usage"]}
    assert counts == {"engine.job.run": 1, "engine.job.cancel": 1}
    assert body["total_events"] == 2
    assert body["since"] is not None
