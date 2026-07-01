"""Cost pre-flight endpoint (#603, ADR-048 dec-4(a)) — POST /v1/engine/schedules/preflight.

Integration at the edge (real route → real ScheduleService.preflight → real pure projection over the
shared ADR-044 price table; fake principal + a create-recording schedules repo). Pins: the exact
"~$X/day" for a known manifest+cron+tokens, unpriced ≠ $0, 422 on a bad manifest/cron, 401 with no
org, and — the read-only contract — that it creates NO schedule.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_execution_engine_service.app.factory import create_app
from oraclous_execution_engine_service.core.dependencies import get_principal, get_schedule_service
from oraclous_execution_engine_service.services.schedule_service import ScheduleService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_MINI = "openrouter/openai/gpt-4o-mini"  # 0.15/0.60 per Mtok
_UNKNOWN = "openrouter/acme/secret-model"


class _RecordingSchedRepo:
    """Records any create so the read-only contract (preflight creates NOTHING) is assertable."""

    def __init__(self) -> None:
        self.creates: list[dict] = []

    async def create(self, **kw: object) -> None:
        self.creates.append(dict(kw))


def _agent(role: str) -> dict:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"x/{role}@1",
        "subgoal": "s",
        "depends_on": [],
        "tools": [],
    }


def _sub(binding: str) -> dict:
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": "s", "owner_organization_id": str(_ORG)},
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "models": [{"role": "primary", "binding": binding, "protocol_shape": "openai-compatible"}],
        "runtime": {"entrypoint": "primary"},
    }


def _team_manifest(roles: list[str]) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "t",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [_agent(r) for r in roles],
        "runtime": {"entrypoint": roles[0]},
    }


async def _client(repo: _RecordingSchedRepo, *, org: uuid.UUID | None = _ORG) -> AsyncClient:
    app = create_app()  # construction only — ASGITransport does not trigger the DB-bind lifespan
    svc = ScheduleService(schedules=repo, jobs=None, provenance=None)  # type: ignore[arg-type]
    app.dependency_overrides[get_schedule_service] = lambda: svc
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


async def test_preflight_prices_a_fleet_and_lists_unpriced_and_creates_nothing() -> None:
    repo = _RecordingSchedRepo()
    body = {
        "manifest": _team_manifest(["a", "b"]),
        "cron": "0 9 * * *",  # daily → 1 fire/day
        "input_data": {"sub_harnesses": {"a": _sub(_MINI), "b": _sub(_UNKNOWN)}},
        "expected_input_tokens": 1_000_000,
        "expected_output_tokens": 1_000_000,
    }
    async with await _client(repo) as c:
        resp = await c.post("/v1/engine/schedules/preflight", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["currency"] == "USD"
    assert data["cadence_fires_per_day"] == pytest.approx(1.0)
    # gpt-4o-mini @ 1M+1M = 0.75/day; the unknown member is unpriced (adds 0, NOT $0)
    assert data["fleet_usd_per_day"] == pytest.approx(0.75)
    assert data["unpriced_members"] == ["b"]
    by_role = {m["role"]: m for m in data["per_member"]}
    assert by_role["a"]["priced"] is True and by_role["a"]["usd_per_day"] == pytest.approx(0.75)
    assert by_role["b"]["priced"] is False and by_role["b"]["usd_per_day"] is None  # unpriced ≠ $0
    assert repo.creates == []  # READ-ONLY: the pre-flight created no schedule


async def test_preflight_invalid_cron_is_422() -> None:
    repo = _RecordingSchedRepo()
    body = {
        "manifest": _team_manifest(["a"]),
        "cron": "not-a-cron",
        "input_data": {"sub_harnesses": {"a": _sub(_MINI)}},
    }
    async with await _client(repo) as c:
        resp = await c.post("/v1/engine/schedules/preflight", json=body)
    assert resp.status_code == 422, resp.text


async def test_preflight_non_team_manifest_is_422() -> None:
    repo = _RecordingSchedRepo()
    # a single-agent (non-team) OHM → not a Team Harness → 422
    body = {"manifest": _sub(_MINI), "cron": "0 9 * * *"}
    async with await _client(repo) as c:
        resp = await c.post("/v1/engine/schedules/preflight", json=body)
    assert resp.status_code == 422, resp.text


async def test_preflight_no_org_principal_is_401() -> None:
    repo = _RecordingSchedRepo()
    body = {"manifest": _team_manifest(["a"]), "cron": "0 9 * * *"}
    async with await _client(repo, org=None) as c:
        resp = await c.post("/v1/engine/schedules/preflight", json=body)
    assert resp.status_code == 401, resp.text
