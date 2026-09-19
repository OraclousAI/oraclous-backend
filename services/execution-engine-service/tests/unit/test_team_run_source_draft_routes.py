"""#1163: the create-team-run route parses + forwards ``team_draft_id``/``team_draft_version``,
and the run read surfaces both (unit; fake service via dependency_overrides, no DB).

RED until ``CreateTeamRunRequest`` declares the two fields (with the ``ge=1`` version floor),
``create_team_run`` passes them through, and ``TeamRunOut`` carries them (always present, null when
the run has no team).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_execution_engine_service.app.factory import create_app
from oraclous_execution_engine_service.core.dependencies import get_principal, get_team_run_service
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _queued_row(manifest: dict[str, Any]) -> EngineTeamRun:
    return EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=manifest,
        sub_harnesses={},
        gate_decisions={},
        state="QUEUED",
        results={},
        paused_at=[],
        error_message=None,
    )


async def _client(service: Any) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_team_run_service] = lambda: service
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


class _CapturingService:
    """Requires the two new keywords explicitly (no default) so a route that does not yet pass
    them raises TypeError — the seam this file is RED on."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(
        self,
        principal: Principal,
        *,
        manifest: dict,
        sub_harnesses: dict,
        gate_decisions: dict,
        workspace_root: str | None = None,
        graph_id: str | None = None,
        inputs: dict | None = None,
        seed_from_run_id: uuid.UUID | None = None,
        team_draft_id: uuid.UUID | None,
        team_draft_version: int | None,
    ) -> EngineTeamRun:
        self.calls.append(
            {"team_draft_id": team_draft_id, "team_draft_version": team_draft_version}
        )
        return _queued_row(manifest)


async def test_post_forwards_the_team_draft_pair_to_the_service() -> None:
    draft_id = uuid.uuid4()
    svc = _CapturingService()
    async with await _client(svc) as c:
        resp = await c.post(
            "/v1/engine/team-runs",
            json={
                "manifest": {"kind": "team"},
                "sub_harnesses": {},
                "gate_decisions": {},
                "team_draft_id": str(draft_id),
                "team_draft_version": 4,
            },
        )
    assert resp.status_code == 202, resp.text
    assert svc.calls == [{"team_draft_id": draft_id, "team_draft_version": 4}]


async def test_post_without_the_pair_forwards_none() -> None:
    svc = _CapturingService()
    async with await _client(svc) as c:
        resp = await c.post(
            "/v1/engine/team-runs",
            json={"manifest": {"kind": "team"}, "sub_harnesses": {}, "gate_decisions": {}},
        )
    assert resp.status_code == 202, resp.text
    assert svc.calls == [{"team_draft_id": None, "team_draft_version": None}]


async def test_a_zero_version_is_rejected_before_the_service_is_called() -> None:
    svc = _CapturingService()
    async with await _client(svc) as c:
        resp = await c.post(
            "/v1/engine/team-runs",
            json={
                "manifest": {"kind": "team"},
                "sub_harnesses": {},
                "gate_decisions": {},
                "team_draft_id": str(uuid.uuid4()),
                "team_draft_version": 0,
            },
        )
    assert resp.status_code == 422, resp.text
    assert svc.calls == []


async def test_a_stale_version_conflict_gives_409_with_a_plain_string_detail() -> None:
    class BadService:
        async def create(
            self,
            *args: Any,
            team_draft_id: uuid.UUID | None,
            team_draft_version: int | None,
            **kwargs: Any,
        ) -> EngineTeamRun:
            raise TeamRunError(
                "the team has changed since it was loaded; reload it and run again",
                409,
                error_type="team_draft_version_conflict",
            )

    async with await _client(BadService()) as c:
        resp = await c.post(
            "/v1/engine/team-runs",
            json={
                "manifest": {"kind": "team"},
                "sub_harnesses": {},
                "gate_decisions": {},
                "team_draft_id": str(uuid.uuid4()),
                "team_draft_version": 1,
            },
        )
    assert resp.status_code == 409, resp.text
    assert isinstance(resp.json()["detail"], str)  # a plain string — the gateway reports CONFLICT


async def test_an_invalid_team_draft_names_the_field_in_loc() -> None:
    class BadService:
        async def create(
            self,
            *args: Any,
            team_draft_id: uuid.UUID | None,
            team_draft_version: int | None,
            **kwargs: Any,
        ) -> EngineTeamRun:
            raise TeamRunError(
                "team_draft_id does not name a draft in your organisation",
                422,
                error_type="invalid_team_draft",
                field="team_draft_id",
            )

    async with await _client(BadService()) as c:
        resp = await c.post(
            "/v1/engine/team-runs",
            json={
                "manifest": {"kind": "team"},
                "sub_harnesses": {},
                "gate_decisions": {},
                "team_draft_id": str(uuid.uuid4()),
                "team_draft_version": 1,
            },
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail[0]["loc"] == ["body", "team_draft_id"], detail
    assert detail[0]["type"] == "invalid_team_draft", detail


async def test_get_team_run_surfaces_the_teams_id_and_version_when_set() -> None:
    rid = uuid.uuid4()
    row = _queued_row({"kind": "team"})
    row.id = rid
    row.state = "SUCCEEDED"
    draft_id = uuid.uuid4()
    row.team_draft_id = draft_id
    row.team_draft_version = 4

    class FakeService:
        async def get(self, run_id: uuid.UUID, principal: Principal) -> EngineTeamRun:
            return row

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "team_draft_id" in body, body
    assert body["team_draft_id"] == str(draft_id)
    assert body["team_draft_version"] == 4


async def test_get_team_run_surfaces_null_when_the_run_has_no_team() -> None:
    rid = uuid.uuid4()
    row = _queued_row({"kind": "team"})
    row.id = rid
    row.state = "SUCCEEDED"

    class FakeService:
        async def get(self, run_id: uuid.UUID, principal: Principal) -> EngineTeamRun:
            return row

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "team_draft_id" in body, body
    assert body["team_draft_id"] is None
    assert "team_draft_version" in body, body
    assert body["team_draft_version"] is None
