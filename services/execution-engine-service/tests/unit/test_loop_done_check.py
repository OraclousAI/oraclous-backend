"""ADR-043 #552 PR-B2 — the CODED done-check the team can never satisfy on its own. A loop converges
only when THREE coded gates clear, each FAIL-CLOSED: (1) COVERAGE — every loop member produced a
non-None output; (2) LANDED ARTIFACTS — the work actually persisted on the bound graph (read via the
ArtifactsClient); (3) the separate-evaluator GRADE clears the declared convergence threshold. An
absent threshold leaves coverage+artifacts as the floor; a malformed one NEVER converges.

RED until ``_make_loop_done_check`` / ``_parse_convergence`` land — imported function-locally.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.manifest import (
    OHMLoop,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRuntime,
    OHMTermination,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_LOOP = OHMLoop(members=["writer", "critic"], routing={})


def _team(convergence: str | None, success_criteria: str = "produce a strong, accurate draft"):
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=[
            OHMMember(role="writer", kind="agent", manifest_ref="o:x/writer@1"),
            OHMMember(role="critic", kind="agent", manifest_ref="o:x/critic@1"),
        ],
        orchestration=OHMOrchestration(
            loops=[_LOOP],
            success_criteria=success_criteria,
            termination=OHMTermination(convergence=convergence),
        ),
        runtime=OHMRuntime(entrypoint="writer"),
    )


class _Artifacts:
    def __init__(self, n: int) -> None:
        self.n = n

    async def list_artifacts(self, graph_id: Any) -> list[dict[str, Any]]:
        return [{"id": str(uuid.uuid4())} for _ in range(self.n)]


class _Evaluate:
    def __init__(self, score: float) -> None:
        self.score = score

    async def evaluate(self, **kw: Any) -> dict[str, Any]:
        return {"score": self.score, "pass": self.score >= 0.7}


def _svc(*, evaluate: Any = None, artifacts: Any = None):
    from oraclous_execution_engine_service.services.team_run_service import TeamRunService

    return TeamRunService(team_runs=object(), evaluate=evaluate, artifacts=artifacts)


def _both_produced() -> dict[str, Any]:
    return {"writer": {"out": "draft"}, "critic": {"out": "review"}}


def test_parse_convergence_table() -> None:
    from oraclous_execution_engine_service.services.team_run_service import _parse_convergence

    assert _parse_convergence("evaluator>=0.8") == (">=", 0.8)
    assert _parse_convergence("  evaluator > 0.5 ") == (">", 0.5)
    assert _parse_convergence("evaluator==1") == ("==", 1.0)
    for malformed in ("evaluator", "score>=0.8", "evaluator>=", ">=0.8", "evaluator~0.8"):
        with pytest.raises(ValueError):
            _parse_convergence(malformed)


async def test_converges_only_when_coverage_artifacts_and_grade_all_clear() -> None:
    gid = str(uuid.uuid4())
    svc = _svc(evaluate=_Evaluate(0.9), artifacts=_Artifacts(2))
    done = svc._make_loop_done_check(_team("evaluator>=0.8"), uuid.uuid4(), gid, _LOOP)
    assert await done(_both_produced()) is True


async def test_coverage_floor_a_missing_member_output_blocks_convergence() -> None:
    gid = str(uuid.uuid4())
    svc = _svc(evaluate=_Evaluate(0.9), artifacts=_Artifacts(2))
    done = svc._make_loop_done_check(_team("evaluator>=0.8"), uuid.uuid4(), gid, _LOOP)
    assert await done({"writer": {"out": "draft"}, "critic": None}) is False


async def test_no_landed_artifacts_blocks_convergence() -> None:
    gid = str(uuid.uuid4())
    svc = _svc(evaluate=_Evaluate(0.9), artifacts=_Artifacts(0))  # nothing persisted
    done = svc._make_loop_done_check(_team("evaluator>=0.8"), uuid.uuid4(), gid, _LOOP)
    assert await done(_both_produced()) is False


async def test_grade_below_threshold_blocks_convergence() -> None:
    gid = str(uuid.uuid4())
    svc = _svc(evaluate=_Evaluate(0.5), artifacts=_Artifacts(2))
    done = svc._make_loop_done_check(_team("evaluator>=0.8"), uuid.uuid4(), gid, _LOOP)
    assert await done(_both_produced()) is False


async def test_unreachable_evaluator_fails_closed_to_not_converged() -> None:
    from oraclous_execution_engine_service.services.evaluate_client import EvaluateClientError

    class _DownEval:
        async def evaluate(self, **kw: Any) -> dict[str, Any]:
            raise EvaluateClientError("judge down")

    gid = str(uuid.uuid4())
    svc = _svc(evaluate=_DownEval(), artifacts=_Artifacts(2))
    done = svc._make_loop_done_check(_team("evaluator>=0.8"), uuid.uuid4(), gid, _LOOP)
    assert await done(_both_produced()) is False  # never converge on an unreachable judge


async def test_malformed_convergence_never_converges_even_when_covered() -> None:
    gid = str(uuid.uuid4())
    svc = _svc(evaluate=_Evaluate(0.9), artifacts=_Artifacts(2))
    done = svc._make_loop_done_check(_team("evaluator~0.8"), uuid.uuid4(), gid, _LOOP)
    assert await done(_both_produced()) is False  # fail-closed on a typo'd threshold


async def test_absent_convergence_leaves_coverage_plus_artifacts_as_the_floor() -> None:
    gid = str(uuid.uuid4())
    svc = _svc(evaluate=_Evaluate(0.0), artifacts=_Artifacts(1))  # no grade gate declared
    done = svc._make_loop_done_check(_team(None), uuid.uuid4(), gid, _LOOP)
    assert await done(_both_produced()) is True
