"""#828 — the two read surfaces a run view polls, and what they must now carry.

Acceptance criterion 1 names ``GET /v1/engine/team-runs/{id}/status`` specifically, and that
endpoint carries no per-member field at all today: ``TeamRunStatusOut`` has ``healthy``, ``state``,
``progress``, ``last_run_at``, ``last_outcome``, ``cost`` and ``grounding_score``. A client wanting
to know which member is working has to make a second call to the full run read. So item 1 is two
changes, not one: write the ``running`` value, and surface it where the issue says to look for it.

Criterion 4 is the tree. ``child_execution_ids`` stays exactly as it is — this is additive, and a
client that only wants the flat list must keep working — with a parallel ``children`` list carrying
the same ids plus the role that produced each.

Wire-shape only: a fake service through ``dependency_overrides``, no DB and no drive. What the
drive actually writes is asserted in ``test_team_run_mid_run_signals.py``.

RED until the schema fields and the route assembly land.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_execution_engine_service.app.factory import create_app
from oraclous_execution_engine_service.core.dependencies import get_principal, get_team_run_service
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _row(**overrides: Any) -> EngineTeamRun:
    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest={"kind": "team"},
        sub_harnesses={},
        gate_decisions={},
        state="RUNNING",
        results={},
        paused_at=[],
        error_message=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


async def _client(service: Any) -> AsyncIterator[AsyncClient]:
    app = create_app()  # construction only — lifespan (DB bind) is not triggered by ASGITransport
    app.dependency_overrides[get_team_run_service] = lambda: service
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


# ── /status carries the per-member map ───────────────────────────────────────────────────────────


async def test_status_reports_which_member_is_running() -> None:
    from oraclous_execution_engine_service.services.team_run_service import TeamRunStatus

    rid = uuid.uuid4()
    started = _dt.datetime(2026, 8, 21, 9, 0, tzinfo=_dt.UTC)
    s = TeamRunStatus(
        team_run_id=rid,
        organisation_id=_ORG,
        healthy=True,
        state="RUNNING",
        progress=33,
        last_run_at=started,
        last_outcome="RUNNING",
        cost_tokens=200,
        member_status={"a": "succeeded", "b": "running", "c": "running"},
        member_timings={
            "a": {
                "started_at": "2026-08-21T09:00:00+00:00",
                "ended_at": "2026-08-21T09:04:00+00:00",
            },
            "b": {"started_at": "2026-08-21T09:04:00+00:00", "ended_at": None},
            "c": {"started_at": "2026-08-21T09:04:00+00:00", "ended_at": None},
        },
    )

    class FakeService:
        async def status(self, run_id: uuid.UUID, principal: Principal) -> TeamRunStatus:
            return s

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["member_status"] == {"a": "succeeded", "b": "running", "c": "running"}
    assert body["member_timings"]["b"]["ended_at"] is None  # in flight
    assert body["member_timings"]["a"]["ended_at"] == "2026-08-21T09:04:00+00:00"
    assert body["progress"] == 33  # the two running members contributed nothing


async def test_status_on_a_pre_migration_row_is_empty_not_a_500() -> None:
    # Every row that predates the migration reads NULL for the new columns. The existing coercing
    # validators on member_status set the precedent: a stale row degrades to {}, never a validation
    # error on a read path a UI polls every fifteen seconds.
    from oraclous_execution_engine_service.services.team_run_service import TeamRunStatus

    rid = uuid.uuid4()
    s = TeamRunStatus(
        team_run_id=rid,
        organisation_id=_ORG,
        healthy=True,
        state="SUCCEEDED",
        progress=100,
        last_run_at=None,
        last_outcome="SUCCEEDED",
        cost_tokens=0,
        member_status=None,  # type: ignore[arg-type]
        member_timings=None,  # type: ignore[arg-type]
    )

    class FakeService:
        async def status(self, run_id: uuid.UUID, principal: Principal) -> TeamRunStatus:
            return s

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}/status")

    assert resp.status_code == 200, resp.text
    assert resp.json()["member_status"] == {}
    assert resp.json()["member_timings"] == {}


# ── /tree maps each child execution to its member role ───────────────────────────────────────────


async def test_tree_maps_every_child_execution_to_a_role() -> None:
    rid, c1, c2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = _row(
        id=rid,
        root_execution_id=rid,
        state="SUCCEEDED",
        child_execution_ids=[str(c1), str(c2)],
        child_execution_roles={str(c1): "researcher", str(c2): "writer"},
    )

    class FakeService:
        async def get(self, run_id: uuid.UUID, principal: Principal) -> EngineTeamRun:
            return row

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}/tree")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_id = {child["execution_id"]: child["role"] for child in body["children"]}
    assert by_id == {str(c1): "researcher", str(c2): "writer"}
    # additive: the flat list is unchanged, so an existing client keeps working
    assert sorted(body["child_execution_ids"]) == sorted([str(c1), str(c2)])


async def test_tree_on_a_pre_migration_row_labels_the_role_unknown() -> None:
    # A run driven before this change has child ids and no role map. The children list must still
    # cover every id — dropping the unlabelled ones would make `children` silently narrower than
    # `child_execution_ids` and turn a missing label into a missing execution.
    rid, c1 = uuid.uuid4(), uuid.uuid4()
    row = _row(
        id=rid,
        root_execution_id=rid,
        state="SUCCEEDED",
        child_execution_ids=[str(c1)],
        child_execution_roles=None,
    )

    class FakeService:
        async def get(self, run_id: uuid.UUID, principal: Principal) -> EngineTeamRun:
            return row

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}/tree")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [child["execution_id"] for child in body["children"]] == [str(c1)]
    assert body["children"][0]["role"] is None
