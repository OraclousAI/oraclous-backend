"""Team-run routes — 202 + error mapping (unit; fake service via dependency_overrides, no DB).

The route is a thin parse → one service call → HTTP map. These pin the async contract at the edge:
POST creates + returns **202** (the worker drives — the request never blocks on a big team), advance
returns 202, and a ``TeamRunError`` maps to its HTTP status.
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
    app = create_app()  # construction only — lifespan (DB bind) is not triggered by ASGITransport
    app.dependency_overrides[get_team_run_service] = lambda: service
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


async def test_post_team_run_returns_202_queued_and_calls_create() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.created: list[dict] = []

        async def create(
            self, principal: Principal, *, manifest: dict, sub_harnesses: dict, gate_decisions: dict
        ) -> EngineTeamRun:
            self.created.append(manifest)
            return _queued_row(manifest)

    svc = FakeService()
    async with await _client(svc) as c:
        resp = await c.post(
            "/v1/engine/team-runs",
            json={"manifest": {"kind": "team"}, "sub_harnesses": {}, "gate_decisions": {}},
        )
    assert resp.status_code == 202  # the worker drives — the request did not block on the team
    assert resp.json()["state"] == "QUEUED"
    assert svc.created == [{"kind": "team"}]  # the route handed the body to the service


async def test_post_team_run_422_emits_structured_detail_for_the_gateway() -> None:
    # #483: a 422 TeamRunError must surface a STRUCTURED detail ([{loc,type,msg}]) so the gateway
    # maps it to VALIDATION_FAILED (field=body, issue=<type>), not the misleading MALFORMED_REQUEST
    # a free-string detail falls back to. The gateway drops the value-reflecting msg.
    class BadService:
        async def create(self, *args: Any, **kwargs: Any) -> EngineTeamRun:
            raise TeamRunError(
                "sub_harness for 'x' exceeds its tools ceiling: …",
                422,
                error_type="ceiling_exceeded",
            )

    async with await _client(BadService()) as c:
        resp = await c.post(
            "/v1/engine/team-runs",
            json={"manifest": {}, "sub_harnesses": {}, "gate_decisions": {}},
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and len(detail) == 1, detail  # the Pydantic-shaped list
    assert detail[0]["loc"] == ["body"] and detail[0]["type"] == "ceiling_exceeded", detail
    assert "msg" in detail[0]  # the gateway extracts loc+type only, dropping the value-bearing msg


async def test_post_team_run_non_422_keeps_string_detail() -> None:
    # a non-422 (e.g. 403) already maps to the right canonical code — keep the plain string detail.
    class BadService:
        async def create(self, *args: Any, **kwargs: Any) -> EngineTeamRun:
            raise TeamRunError("authenticated principal has no organisation scope", 403)

    async with await _client(BadService()) as c:
        resp = await c.post(
            "/v1/engine/team-runs",
            json={"manifest": {}, "sub_harnesses": {}, "gate_decisions": {}},
        )
    assert resp.status_code == 403
    assert isinstance(resp.json()["detail"], str)  # string detail, not the structured list


async def test_get_tree_returns_root_and_children() -> None:  # ADR-037 D3 / #471
    rid, c1, c2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = _queued_row({"kind": "team"})
    row.id = rid
    row.root_execution_id = rid
    row.child_execution_ids = [str(c1), str(c2)]
    row.state = "SUCCEEDED"

    class FakeService:
        async def get(self, run_id: uuid.UUID, principal: Principal) -> EngineTeamRun:
            return row

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}/tree")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["team_run_id"] == str(rid)
    assert body["root_execution_id"] == str(rid)  # the run is its own tree root
    assert sorted(body["child_execution_ids"]) == sorted([str(c1), str(c2)])
    assert body["state"] == "SUCCEEDED"


async def test_get_tree_cross_org_is_404() -> None:  # H1/H4
    class CrossOrgService:
        async def get(self, run_id: uuid.UUID, principal: Principal) -> EngineTeamRun:
            raise TeamRunError("team run not found", 404)  # org-scoped get → 404 for another org

    async with await _client(CrossOrgService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{uuid.uuid4()}/tree")
    assert resp.status_code == 404  # a cross-org tree id is not-found, never a leak


async def test_get_status_returns_progress_health_cost() -> None:  # ADR-037 D5 / #472
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
        cost_tokens=200,
    )

    class FakeService:
        async def status(self, run_id: uuid.UUID, principal: Principal) -> TeamRunStatus:
            return s

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["progress"] == 100 and body["healthy"] is True and body["state"] == "SUCCEEDED"
    assert body["cost"] == {"tokens": 200, "usd": None}  # raw metering; usd priced read-time later


async def test_get_status_cross_org_is_404() -> None:  # H3
    class CrossOrgService:
        async def status(self, run_id: uuid.UUID, principal: Principal) -> object:
            raise TeamRunError("team run not found", 404)  # org-scoped status → 404 for another org

    async with await _client(CrossOrgService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{uuid.uuid4()}/status")
    assert resp.status_code == 404  # a cross-org status id is not-found, never a leak


async def test_advance_team_run_returns_202() -> None:
    class FakeService:
        async def advance(
            self, run_id: uuid.UUID, principal: Principal, gate_decisions: dict
        ) -> EngineTeamRun:
            return _queued_row({"kind": "team"})

    async with await _client(FakeService()) as c:
        resp = await c.post(
            f"/v1/engine/team-runs/{uuid.uuid4()}/advance",
            json={"gate_decisions": {"approval": "approve"}},
        )
    assert resp.status_code == 202  # advance re-queues; the worker drives the resume
    assert resp.json()["state"] == "QUEUED"
