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


async def test_post_team_run_maps_teamrunerror_to_its_http_status() -> None:
    class BadService:
        async def create(self, *args: Any, **kwargs: Any) -> EngineTeamRun:
            raise TeamRunError("manifest is not a Team Harness", 422)

    async with await _client(BadService()) as c:
        resp = await c.post(
            "/v1/engine/team-runs",
            json={"manifest": {}, "sub_harnesses": {}, "gate_decisions": {}},
        )
    assert resp.status_code == 422  # TeamRunError(status) -> HTTPException(status), not a 500


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
