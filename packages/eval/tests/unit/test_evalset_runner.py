"""#597 (ADR-047 §7) — Layer-3: the ship-bar runner (sample-N, judge-debias, K-of-N).

Fake judges + fake compile/run prove the runner's logic deterministically: the split rubric (plan-
adequacy gates the run), the Layer-1 guardrails gate the judge, K-of-N consensus, the median-score
floor, the judge-variance → inconclusive axis, and the recorded reference set's shape.
"""

from __future__ import annotations

import json
import uuid

import pytest
from oraclous_eval.evalset import (
    CompiledPlan,
    EvalSetManifest,
    EvalSetRunner,
    Objective,
    ShipBar,
    _rotate_dimensions,
)
from oraclous_eval.reference import reference_eval_set
from oraclous_eval.types import Dimension, Rubric

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_CATALOG = ["web-research", "write"]


class _FakeJudge:
    """An ``EvalJudge`` returning a fixed score for each dimension (deterministic panel control)."""

    def __init__(self, score: float) -> None:
        self._score = score

    async def complete_json(self, *, system: str, user: str) -> str:
        return json.dumps({"score": self._score, "reason": "fake"})

    async def complete_text(self, *, system: str, user: str) -> str:
        return "fake"


class _ErroringJudge:
    """An ``EvalJudge`` whose calls RAISE — the evaluator fail-softs each dimension to null, so the
    Verdict has empty ``metrics_computed`` (a judge outage, NOT a real low score)."""

    async def complete_json(self, *, system: str, user: str) -> str:
        raise RuntimeError("judge unreachable")

    async def complete_text(self, *, system: str, user: str) -> str:
        raise RuntimeError("judge unreachable")


def _good_manifest() -> dict[str, object]:
    return {
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:c/r@1",
                "tools": ["web-research"],
                "depends_on": [],
            },
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:c/w@1",
                "tools": ["write"],
                "depends_on": ["researcher"],
            },
        ],
        "orchestration": {"style": "linear", "success_criteria": "done"},
        "budget": {"max_tokens_total": 500_000, "max_tokens_per_member": 100_000},
    }


def _cyclic_manifest() -> dict[str, object]:
    return {
        "members": [
            {"role": "a", "kind": "agent", "manifest_ref": "org:c/a@1", "depends_on": ["b"]},
            {"role": "b", "kind": "agent", "manifest_ref": "org:c/b@1", "depends_on": ["a"]},
        ],
        "orchestration": {"style": "linear", "success_criteria": "done"},
    }


def _compile_good(_prose: str):
    async def _c(prose: str) -> CompiledPlan:
        return CompiledPlan(manifest=_good_manifest(), catalog=_CATALOG)

    return _c


def _compile_returning(manifest: dict[str, object]):
    async def _c(prose: str) -> CompiledPlan:
        return CompiledPlan(manifest=manifest, catalog=_CATALOG)

    return _c


async def _run_fn(_manifest: dict[str, object]) -> str:
    return "a real deliverable produced by the compiled team"


def _one_objective() -> Objective:
    rubric = Rubric(
        pass_threshold=0.7,
        dimensions=[
            Dimension(name="d1", prompt="score it"),
            Dimension(name="d2", prompt="score it"),
        ],
    )
    return Objective(id="obj", prose="do the thing", plan_rubric=rubric, run_rubric=rubric)


def _manifest(objective: Objective, bar: ShipBar) -> EvalSetManifest:
    return EvalSetManifest(name="t", objectives=[objective], ship_bar=bar)


async def test_all_samples_pass_ships() -> None:
    runner = EvalSetRunner(
        _manifest(_one_objective(), ShipBar(n_samples=3, k_pass=2)),
        judges=[_FakeJudge(0.9), _FakeJudge(0.85), _FakeJudge(0.95)],
        compile=_compile_returning(_good_manifest()),
        run=_run_fn,
        owner_organization_id=_ORG,
    )
    result = await runner.run()
    assert result.overall_passed is True
    obj = result.objectives[0]
    assert obj.passed and obj.recommendation == "ship"
    assert obj.pass_count == 3 and obj.consensus_ratio == 1.0


async def test_a_guardrail_blocked_plan_never_reaches_the_judge() -> None:
    runner = EvalSetRunner(
        _manifest(_one_objective(), ShipBar(n_samples=3, k_pass=2)),
        judges=[_FakeJudge(0.99)],  # would pass — but the cyclic plan is blocked first
        compile=_compile_returning(_cyclic_manifest()),
        run=_run_fn,
        owner_organization_id=_ORG,
    )
    result = await runner.run()
    obj = result.objectives[0]
    assert obj.passed is False
    assert all(s.blocked_by_guardrails for s in obj.samples)
    assert all("F-DAG-INVALID" in r for s in obj.samples for r in s.guardrail_reasons)


async def test_plan_adequacy_failure_skips_the_run() -> None:
    # a low plan score → the run is never attempted (run stays None).
    runner = EvalSetRunner(
        _manifest(_one_objective(), ShipBar(n_samples=3, k_pass=2)),
        judges=[_FakeJudge(0.2)],
        compile=_compile_returning(_good_manifest()),
        run=_run_fn,
        owner_organization_id=_ORG,
    )
    result = await runner.run()
    obj = result.objectives[0]
    assert obj.passed is False
    assert all(s.plan is not None and not s.plan.passed and s.run is None for s in obj.samples)


async def test_k_of_n_consensus_below_k_does_not_ship() -> None:
    # plan passes, run scored by a panel that gives a LOW median → samples fail; pass_count < k.
    runner = EvalSetRunner(
        _manifest(_one_objective(), ShipBar(n_samples=3, k_pass=2, min_score=0.7)),
        judges=[_FakeJudge(0.55), _FakeJudge(0.5), _FakeJudge(0.6)],  # median 0.55 < 0.7
        compile=_compile_returning(_good_manifest()),
        run=_run_fn,
        owner_organization_id=_ORG,
    )
    result = await runner.run()
    obj = result.objectives[0]
    assert obj.passed is False and obj.pass_count == 0


async def test_high_judge_variance_is_inconclusive() -> None:
    # the panel disagrees wildly (0.95 vs 0.1) — even if the median clears, variance > ceiling.
    runner = EvalSetRunner(
        _manifest(_one_objective(), ShipBar(n_samples=3, k_pass=2, max_variance=0.1)),
        judges=[_FakeJudge(0.95), _FakeJudge(0.1), _FakeJudge(0.9)],
        compile=_compile_returning(_good_manifest()),
        run=_run_fn,
        owner_organization_id=_ORG,
    )
    result = await runner.run()
    obj = result.objectives[0]
    assert obj.variance > 0.1
    assert obj.passed is False and obj.recommendation == "inconclusive"


async def test_plan_only_objective_needs_no_run() -> None:
    plan_rubric = Rubric(dimensions=[Dimension(name="d", prompt="x")], pass_threshold=0.7)
    obj = Objective(id="plan-only", prose="x", plan_rubric=plan_rubric, run_rubric=None)
    runner = EvalSetRunner(
        _manifest(obj, ShipBar(n_samples=3, k_pass=2)),
        judges=[_FakeJudge(0.9)],
        compile=_compile_returning(_good_manifest()),
        run=None,  # no run callback
        owner_organization_id=_ORG,
    )
    result = await runner.run()
    o = result.objectives[0]
    assert o.passed is True and all(s.run is None for s in o.samples)


def test_ship_bar_rejects_k_above_n() -> None:
    with pytest.raises(ValueError, match="k_pass"):
        ShipBar(n_samples=3, k_pass=4)


def test_dimension_rotation_debiases_order() -> None:
    rubric = Rubric(
        dimensions=[Dimension(name=n, prompt="x") for n in ("a", "b", "c")], pass_threshold=0.7
    )
    assert [d.name for d in _rotate_dimensions(rubric, 0).dimensions] == ["a", "b", "c"]
    assert [d.name for d in _rotate_dimensions(rubric, 1).dimensions] == ["b", "c", "a"]
    assert [d.name for d in _rotate_dimensions(rubric, 2).dimensions] == ["c", "a", "b"]


def test_an_empty_judge_panel_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one judge"):
        EvalSetRunner(_manifest(_one_objective(), ShipBar()), judges=[], compile=_compile_good(""))


async def test_a_judge_outage_is_inconclusive_not_a_fabricated_low_score() -> None:
    # the whole panel errors → each Verdict fail-softs to 0.0 with empty metrics_computed. That MUST
    # NOT read as "the team is terrible" (escalate) — it is a judge OUTAGE → inconclusive, and the
    # fabricated 0.0s must never enter the median/variance (else variance 0.0 slips the ceiling).
    runner = EvalSetRunner(
        _manifest(_one_objective(), ShipBar(n_samples=3, k_pass=2)),
        judges=[_ErroringJudge(), _ErroringJudge()],
        compile=_compile_returning(_good_manifest()),
        run=_run_fn,
        owner_organization_id=_ORG,
    )
    result = await runner.run()
    obj = result.objectives[0]
    assert obj.passed is False and obj.recommendation == "inconclusive"
    assert all(s.unevaluable and s.judge_unavailable for s in obj.samples)
    assert all(s.plan is not None and s.plan.judges_scored == 0 for s in obj.samples)


async def test_a_partial_judge_outage_excludes_the_errored_judge() -> None:
    # one real judge + one erroring judge in a 2-panel: below a strict majority → judge_unavailable,
    # and the errored judge's 0.0 is excluded (judges_scored == 1, not 2).
    runner = EvalSetRunner(
        _manifest(_one_objective(), ShipBar(n_samples=3, k_pass=2)),
        judges=[_FakeJudge(0.9), _ErroringJudge()],
        compile=_compile_returning(_good_manifest()),
        run=_run_fn,
        owner_organization_id=_ORG,
    )
    result = await runner.run()
    obj = result.objectives[0]
    assert obj.recommendation == "inconclusive"
    assert all(s.plan is not None and s.plan.judges_scored == 1 for s in obj.samples)
    assert all(s.plan and s.plan.judge_scores == [0.9] for s in obj.samples)  # the 0.0 is excluded


async def test_a_raising_compile_errors_the_sample_without_aborting_the_sweep() -> None:
    async def _boom(_prose: str):
        raise RuntimeError("the gateway is down")

    runner = EvalSetRunner(
        _manifest(_one_objective(), ShipBar(n_samples=3, k_pass=2)),
        judges=[_FakeJudge(0.9)],
        compile=_boom,
        run=_run_fn,
        owner_organization_id=_ORG,
    )
    result = await runner.run()  # must NOT raise — one bad sample never kills the run
    obj = result.objectives[0]
    assert all(s.errored and s.error_reason == "RuntimeError" for s in obj.samples)
    assert obj.passed is False and obj.recommendation == "inconclusive"


async def test_a_raising_run_errors_the_sample_after_the_plan_scored() -> None:
    async def _boom(_manifest: dict[str, object]) -> str:
        raise RuntimeError("the run failed")

    runner = EvalSetRunner(
        _manifest(_one_objective(), ShipBar(n_samples=3, k_pass=2)),
        judges=[_FakeJudge(0.9)],
        compile=_compile_returning(_good_manifest()),
        run=_boom,
        owner_organization_id=_ORG,
    )
    result = await runner.run()
    obj = result.objectives[0]
    assert all(s.errored and s.plan is not None for s in obj.samples)  # plan scored, run errored
    assert obj.recommendation == "inconclusive"


def test_the_reference_eval_set_is_recorded_data() -> None:
    es = reference_eval_set()
    assert len(es.objectives) >= 15
    assert all(o.run_rubric is not None for o in es.objectives)  # every objective has both rubrics
    assert all(o.plan_rubric.dimensions for o in es.objectives)
    # the recorded ship-bar (ADR-047 §7 founder-decision #3)
    assert es.ship_bar.n_samples >= 3 and es.ship_bar.objective_pass_ratio == 0.8
    assert es.ship_bar.k_pass <= es.ship_bar.n_samples
    # unique objective ids
    assert len({o.id for o in es.objectives}) == len(es.objectives)
