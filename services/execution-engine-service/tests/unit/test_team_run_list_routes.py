"""GET /v1/engine/team-runs — the org-scoped team-run LIST route (#633; unit, fake service, no DB).

The route is a thin parse → one ``service.list_for_org`` call → HTTP map. These pin the edge
contract: the ``{team_runs, total}`` wire shape, the LEAN list row (never manifest/results), the
``team_name`` dig + ``partial`` derivation, that ``state``/``limit``/``offset`` are forwarded, and
that an unknown ``state`` is a native 422 — all with the service faked via dependency_overrides.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_execution_engine_service.app.factory import create_app
from oraclous_execution_engine_service.core.dependencies import get_principal, get_team_run_service
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _row(**over: Any) -> dict[str, Any]:
    """One projected list row (the shape ``repo.list_for_org`` returns — dicts, never ORM rows)."""
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "state": "PAUSED",
        "team_name": "studio",
        "created_at": None,
        "updated_at": None,
        "paused_at": ["gate-a"],
        "member_status": {"researcher": "succeeded"},
        "schedule_id": None,
        "cost_tokens": 100,
    }
    base.update(over)
    return base


async def _client(service: Any) -> AsyncIterator[AsyncClient]:
    app = create_app()  # construction only — lifespan (DB bind) is not triggered by ASGITransport
    app.dependency_overrides[get_team_run_service] = lambda: service
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


class _FakeService:
    def __init__(self, rows: list[dict[str, Any]], total: int) -> None:
        self._rows = rows
        self._total = total
        self.calls: list[dict[str, Any]] = []

    async def list_for_org(
        self,
        principal: Principal,
        *,
        states: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls.append({"states": states, "limit": limit, "offset": offset})
        return self._rows, self._total


async def test_list_returns_the_team_runs_and_total_wire_shape() -> None:
    rows = [_row(state="SUCCEEDED", paused_at=[]), _row(state="PAUSED")]
    async with await _client(_FakeService(rows, total=7)) as c:
        resp = await c.get("/v1/engine/team-runs")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 7  # the FULL matching count, not the page length
    assert len(body["team_runs"]) == 2
    item = body["team_runs"][1]
    assert item["state"] == "PAUSED" and item["team_name"] == "studio"
    assert item["paused_at"] == ["gate-a"] and item["member_status"] == {"researcher": "succeeded"}
    assert item["cost_tokens"] == 100 and item["schedule_id"] is None


async def test_list_row_is_lean_never_manifest_or_results() -> None:
    async with await _client(_FakeService([_row()], total=1)) as c:
        body = (await c.get("/v1/engine/team-runs")).json()
    item = body["team_runs"][0]
    # a runs-table row, NOT the full readout — the heavy fields never appear in a list row
    for forbidden in ("manifest", "results", "sub_harnesses"):
        assert forbidden not in item, f"{forbidden} leaked into a list row"


async def test_partial_is_derived_for_a_cost_budget_row() -> None:
    async with await _client(_FakeService([_row(state="COST_BUDGET")], total=1)) as c:
        body = (await c.get("/v1/engine/team-runs")).json()
    assert body["team_runs"][0]["partial"] is True  # #585: a pooled-budget halt is partial


async def test_state_limit_offset_are_forwarded_to_the_service() -> None:
    svc = _FakeService([], total=0)
    async with await _client(svc) as c:
        resp = await c.get("/v1/engine/team-runs?state=PAUSED&state=RUNNING&limit=10&offset=20")
    assert resp.status_code == 200, resp.text
    assert svc.calls == [{"states": ["PAUSED", "RUNNING"], "limit": 10, "offset": 20}]


async def test_defaults_are_limit_50_offset_0_no_state() -> None:
    svc = _FakeService([], total=0)
    async with await _client(svc) as c:
        await c.get("/v1/engine/team-runs")
    assert svc.calls == [{"states": None, "limit": 50, "offset": 0}]


async def test_an_unknown_state_is_a_422() -> None:
    # the state filter is a native FastAPI enum → an unknown value never reaches the service
    svc = _FakeService([], total=0)
    async with await _client(svc) as c:
        resp = await c.get("/v1/engine/team-runs?state=NOPE")
    assert resp.status_code == 422, resp.text
    assert svc.calls == []  # rejected at the edge, the service was never called


async def test_a_teamrun_error_maps_to_its_status_not_a_500() -> None:
    # the route must translate a service TeamRunError (e.g. the no-org 403) to its HTTP status, like
    # every sibling route — an un-mapped TeamRunError would surface as a 500 (self-review LOW).
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    class _NoOrgService:
        async def list_for_org(self, *a: Any, **k: Any) -> tuple[list[dict[str, Any]], int]:
            raise TeamRunError("authenticated principal has no organisation scope", 403)

    async with await _client(_NoOrgService()) as c:
        resp = await c.get("/v1/engine/team-runs")
    assert resp.status_code == 403, resp.text  # the contracted status, never a 500


async def test_the_collection_get_is_not_captured_as_an_id() -> None:
    # the collection route is registered BEFORE /team-runs/{id}; a bare GET /team-runs lists — it
    # must NOT dispatch to the get-by-id handler (which would treat "team-runs" as a uuid path arg)
    called: dict[str, bool] = {"listed": False, "got": False}

    class _Svc(_FakeService):
        async def list_for_org(self, *a: Any, **k: Any) -> tuple[list[dict[str, Any]], int]:
            called["listed"] = True
            return [], 0

        async def get(self, *a: Any, **k: Any) -> Any:  # would only fire if routed to get-by-id
            called["got"] = True
            raise AssertionError("GET /team-runs must not dispatch to get-by-id")

    async with await _client(_Svc([], 0)) as c:
        resp = await c.get("/v1/engine/team-runs")
    assert resp.status_code == 200 and called["listed"] and not called["got"]
