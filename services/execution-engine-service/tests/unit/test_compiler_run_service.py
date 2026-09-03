"""CompilerRunService (#635) — unit, the TeamRunService seam faked, no DB.

Pins the Describe door's assembly contract: the compiler team is built server-side with the #596
seeded catalog baked into the manifest-drafter's subgoal (#709/#713), the caller's BYOM models are
bound into the manifest AND every sub-harness, the optional Describe fields fold into the planner's
objective, and the run is submitted through the SAME create path (gate_decisions {}, graph_id
passthrough).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.domain.compiler_onramp import (
    compose_objective,
    draft_catalog,
)
from oraclous_execution_engine_service.services.compiler_run_service import CompilerRunService
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


def _model() -> dict[str, Any]:
    return {
        "role": "primary",
        "binding": "openrouter/openai/gpt-4o-mini",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": "cred-1"},
    }


class _FakeTeamRuns:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create(self, principal: Principal, **kw: Any) -> Any:
        self.created.append(kw)

        class _Row:
            id = uuid.uuid4()
            state = "QUEUED"

        return _Row()


async def test_compiler_run_assembles_binds_and_submits_through_the_same_path() -> None:
    team_runs = _FakeTeamRuns()
    svc = CompilerRunService(team_runs=team_runs)  # type: ignore[arg-type]

    row = await svc.create(
        _principal(),
        objective="Research this week's most-cited AI papers.",
        models=[_model()],
        graph_id="graph-1",
    )
    assert row.state == "QUEUED"
    submitted = team_runs.created[0]
    manifest = submitted["manifest"]
    subs = submitted["sub_harnesses"]

    # the three-member compiler team, assembled server-side (ADR-047 — no new runtime). #709
    # deleted capability-surveyor: nothing read its retyped output any more.
    roles = {m["role"] for m in manifest["members"]}
    assert roles == {"planner", "manifest-drafter", "reviewer"}

    # the caller's BYOM models are bound into the manifest AND every sub-harness
    assert manifest["models"] == [_model()]
    assert set(subs) == roles
    for sub in subs.values():
        assert sub["models"] == [_model()]

    # #713: the #596 seeded catalog (with descriptions) is baked into the DRAFTER's subgoal now —
    # the surveyor is gone, so this is where "a real seeded slug appears" is checked.
    drafter = next(m for m in manifest["members"] if m["role"] == "manifest-drafter")
    catalog = draft_catalog()
    assert catalog, "the seed inventory is non-empty"
    assert catalog[0] in drafter["subgoal"]

    # submitted through the SAME create path: no pre-seeded gates, graph passthrough
    assert submitted["gate_decisions"] == {}
    assert submitted["graph_id"] == "graph-1"


async def test_describe_fields_fold_into_the_planner_objective() -> None:
    team_runs = _FakeTeamRuns()
    svc = CompilerRunService(team_runs=team_runs)  # type: ignore[arg-type]

    await svc.create(
        _principal(),
        objective="Assess the market.",
        inputs={"tickers": ["A", "B"]},
        constraints="Cite every claim.",
        success_criteria="At least 10 sources.",
        models=[_model()],
    )
    manifest = team_runs.created[0]["manifest"]
    planner = next(m for m in manifest["members"] if m["role"] == "planner")
    for fragment in ("Assess the market.", "tickers", "Cite every claim.", "At least 10 sources."):
        assert fragment in planner["subgoal"]


async def test_compose_objective_skips_absent_fields() -> None:
    assert compose_objective("Do the thing.") == "Do the thing."
    assert "Constraints" not in compose_objective("x", constraints="   ")


async def test_missing_models_is_a_422_and_nothing_submits() -> None:
    team_runs = _FakeTeamRuns()
    svc = CompilerRunService(team_runs=team_runs)  # type: ignore[arg-type]
    with pytest.raises(TeamRunError) as exc:
        await svc.create(_principal(), objective="x", models=[])
    assert exc.value.status_code == 422
    assert team_runs.created == []


async def test_invalid_model_binding_is_a_422() -> None:
    svc = CompilerRunService(team_runs=_FakeTeamRuns())  # type: ignore[arg-type]
    with pytest.raises(TeamRunError) as exc:
        await svc.create(_principal(), objective="x", models=[{"binding": 42, "role": []}])
    assert exc.value.status_code == 422


async def test_no_org_principal_is_a_403() -> None:
    svc = CompilerRunService(team_runs=_FakeTeamRuns())  # type: ignore[arg-type]
    with pytest.raises(TeamRunError) as exc:
        await svc.create(_principal(org=None), objective="x", models=[_model()])
    assert exc.value.status_code == 403
