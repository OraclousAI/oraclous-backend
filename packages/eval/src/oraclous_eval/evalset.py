"""#597 (ADR-047 §7) — Layer-3: the reference-objective eval-set + the ship-bar runner.

A non-deterministic NL→team generator is not proven by one run. This Layer is the ACCEPTANCE
INSTRUMENT: a recorded set of prose objectives, each scored over N samples by a de-biased panel of
judges, with a K-of-N ship-bar — "useful for early adopters", explicitly NOT "perfect for arbitrary
prose" (ADR-047 §7 / founder-decision #3). The runner is generator/judge-AGNOSTIC: it takes
injected ``compile`` / ``run`` callbacks (the deployed compiler-team + team-run on the gateway) and
a panel of ``EvalJudge`` (the same ``OpenAIEvalJudge`` KRS's ``/internal/v1/evaluate`` wraps), so it
unit-tests with fakes and drives the real deployed stack unchanged.

The rubric is SPLIT (ADR-047 §7): **plan-adequacy** is judged on the manifest ALONE (cheap, catching
a structurally-wrong team before a costly run — gated first by the Layer-1 guardrails),
and **run-outcome** is judged on the executed deliverable, only for objectives that pass plan-
adequacy. Ship-bar = fail-closed on three axes: K-of-N consensus, a median-score floor, and a
judge-variance ceiling (wild disagreement → inconclusive, never a silent ship).
"""

from __future__ import annotations

import asyncio
import json
import statistics
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oraclous_eval.evaluator import RubricEvaluator
from oraclous_eval.guardrails import run_plan_guardrails
from oraclous_eval.judge import EvalJudge
from oraclous_eval.types import Evaluated, Rubric, Verdict

_ORG_NS = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class CompiledPlan:
    """What the injected ``compile`` callback returns for a prose objective: the compiled team
    manifest (a dict) + the surveyed catalog the Layer-1 guardrails check the tools against."""

    manifest: dict[str, object]
    catalog: list[object]


# the deployed wiring is injected (kept out of the runner so it unit-tests with fakes):
#   compile(objective_prose) -> CompiledPlan   (drive the compiler-team on :8006, extract the team)
#   run(compiled_manifest)   -> deliverable    (run the compiled team on :8006, return its output)
CompileFn = Callable[[str], Awaitable[CompiledPlan]]
RunFn = Callable[[dict[str, object]], Awaitable[str]]


class Objective(BaseModel):
    """One reference objective: the prose the compiler lowers, plus the SPLIT rubric — plan-adequacy
    (judged on the manifest) and an optional run-outcome (judged on the deliverable)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    prose: str
    plan_rubric: Rubric
    run_rubric: Rubric | None = None


class ShipBar(BaseModel):
    """The recorded ship-bar (data, not a hard-coded assertion): K-of-N per objective, a
    median-score floor, a judge-variance ceiling, and the fraction of objectives that pass."""

    model_config = ConfigDict(extra="forbid")

    n_samples: int = Field(default=3, ge=1)  # N — the default the reference set records is ≥3
    k_pass: int = Field(default=2, ge=1)  # K-of-N samples that must pass
    min_score: float = Field(default=0.7, ge=0.0, le=1.0)  # median-score floor
    max_variance: float = Field(default=0.2, ge=0.0)  # judge-disagreement ceiling (std)
    objective_pass_ratio: float = Field(default=0.8, ge=0.0, le=1.0)  # ≥80% of objectives pass

    @model_validator(mode="after")
    def _k_within_n(self) -> ShipBar:
        if self.k_pass > self.n_samples:
            raise ValueError("ship-bar k_pass must be ≤ n_samples")
        return self


class EvalSetManifest(BaseModel):
    """The declarative, version-controlled eval-set: the objectives + the recorded ship-bar."""

    model_config = ConfigDict(extra="forbid")

    name: str
    objectives: list[Objective]
    ship_bar: ShipBar = Field(default_factory=ShipBar)


class DebiasedScore(BaseModel):
    """A panel of judges' aggregate over one output: the median score, their disagreement (std), and
    the pass-consensus ratio. De-biased by rotating the rubric-dimension order per judge."""

    model_config = ConfigDict(extra="forbid")

    median_score: float
    variance: float  # population std of the AVAILABLE judges' scores — the disagreement signal
    consensus: float  # fraction of AVAILABLE judges that returned pass
    passed: bool
    judge_scores: list[float] = Field(default_factory=list)  # only judges that really scored
    judges_scored: int = 0  # judges that produced ≥1 real dimension score (not a fail-soft 0.0)
    judges_total: int = 0

    @property
    def judge_unavailable(self) -> bool:
        """A judge error/timeout fail-softs to a 0.0 Verdict (ADR-037 evaluator) that is
        indistinguishable from a real low score. When fewer than a STRICT MAJORITY of the panel
        produced a real score, this aggregate is NOT a verdict — an outage must not masquerade as a
        confident low score (the 'never silent ship' axis). Excluded judges are kept off the median
        and variance, so a full outage cannot fabricate variance 0.0 and slip the ceiling."""
        return self.judges_scored * 2 <= self.judges_total


class SampleVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    passed: bool
    blocked_by_guardrails: bool = False
    guardrail_reasons: list[str] = Field(default_factory=list)
    errored: bool = False  # the injected compile/run RAISED — infra, not a clean negative verdict
    error_reason: str = ""  # a coarse label (the exception TYPE) — never customer text (ADR-037 H5)
    plan: DebiasedScore | None = None
    run: DebiasedScore | None = None

    @property
    def score(self) -> float:
        """The sample's representative score — the run-outcome if it ran, else plan-adequacy, else 0
        (guardrail-blocked)."""
        if self.run is not None:
            return self.run.median_score
        if self.plan is not None:
            return self.plan.median_score
        return 0.0

    @property
    def judge_unavailable(self) -> bool:
        return any(d.judge_unavailable for d in (self.plan, self.run) if d is not None)

    @property
    def unevaluable(self) -> bool:
        """Could NOT be confidently evaluated — an infra error or a judge outage. (A guardrail block
        is NOT unevaluable: it is a real, confident negative — the team is structurally broken.)"""
        return self.errored or self.judge_unavailable


class ShipBarVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective_id: str
    samples: list[SampleVerdict]
    pass_count: int
    consensus_ratio: float  # pass_count / N
    median_score: float
    variance: float  # max judge-disagreement across the samples
    passed: bool
    recommendation: Literal["ship", "revise", "escalate", "inconclusive"]


class EvalSetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    objectives: list[ShipBarVerdict]
    overall_passed: bool
    summary: dict[str, object] = Field(default_factory=dict)


def _rotate_dimensions(rubric: Rubric, by: int) -> Rubric:
    """De-bias: rotate the rubric-dimension order per judge so positional bias does not correlate
    across the panel (deterministic — no RNG, so a resumed/replayed run is identical)."""
    dims = list(rubric.dimensions)
    if len(dims) <= 1:
        return rubric
    shift = by % len(dims)
    return rubric.model_copy(update={"dimensions": dims[shift:] + dims[:shift]})


class EvalSetRunner:
    """Run the eval-set: sample each objective N times, score each sample with the de-biased judge
    panel (plan-adequacy then run-outcome), and apply the K-of-N ship-bar. Pure async orchestrator —
    all I/O (compile, run, judge) is injected."""

    def __init__(
        self,
        manifest: EvalSetManifest,
        judges: list[EvalJudge],
        *,
        compile: CompileFn,
        run: RunFn | None = None,
        owner_organization_id: uuid.UUID | None = None,
    ) -> None:
        if not judges:
            raise ValueError("the eval-set needs at least one judge")
        self._manifest = manifest
        self._judges = judges
        self._compile = compile
        self._run = run
        self._org = owner_organization_id or uuid.UUID(_ORG_NS)

    async def _debias_score(self, rubric: Rubric, output: str, target_ref: str) -> DebiasedScore:
        evaluated = Evaluated(
            target_kind="member_output", target_ref=target_ref, organisation_id=str(self._org)
        )
        verdicts: list[Verdict] = []
        for i, judge in enumerate(self._judges):
            rotated = _rotate_dimensions(rubric, i)
            v = await RubricEvaluator(judge).evaluate(
                rubric=rotated, target_output=output, evaluated=evaluated
            )
            verdicts.append(v)
        # EXCLUDE judges that produced NO real dimension score: an error/timeout/malformed-response
        # fail-softs to score 0.0 with an EMPTY metrics_computed (ADR-037 evaluator), which is
        # indistinguishable from a real low score. Letting that 0.0 enter the median would fabricate
        # a confident low verdict — and a full outage's identical 0.0s would yield variance 0.0,
        # silently slipping the variance ceiling. So we aggregate over the available judges only.
        available = [v for v in verdicts if v.metrics_computed]
        if not available:
            return DebiasedScore(
                median_score=0.0,
                variance=0.0,
                consensus=0.0,
                passed=False,
                judge_scores=[],
                judges_scored=0,
                judges_total=len(verdicts),
            )
        scores = [v.score for v in available]
        median = statistics.median(scores)
        variance = statistics.pstdev(scores) if len(scores) > 1 else 0.0
        consensus = sum(1 for v in available if v.passed) / len(available)
        # fail-closed: a passing sample needs BOTH a score floor AND a judge majority.
        passed = median >= rubric.pass_threshold and consensus >= 0.5
        return DebiasedScore(
            median_score=round(median, 4),
            variance=round(variance, 4),
            consensus=round(consensus, 4),
            passed=passed,
            judge_scores=[round(s, 4) for s in scores],
            judges_scored=len(available),
            judges_total=len(verdicts),
        )

    async def _score_sample(self, objective: Objective, index: int) -> SampleVerdict:
        # the injected compile is infra: a raised error fails THIS sample, never the whole sweep.
        try:
            plan = await self._compile(objective.prose)
        except Exception as exc:  # noqa: BLE001 — generator/infra failure → an errored sample
            return SampleVerdict(
                index=index, passed=False, errored=True, error_reason=type(exc).__name__
            )
        # Layer-1 deterministic guardrails FIRST — a structurally-broken team never reaches a judge.
        guard = run_plan_guardrails(
            plan.manifest, owner_organization_id=self._org, catalog=plan.catalog
        )
        if guard.would_block:
            return SampleVerdict(
                index=index,
                passed=False,
                blocked_by_guardrails=True,
                guardrail_reasons=guard.blocking,
            )
        # plan-adequacy: judge the MANIFEST text alone (cheap; catches a wrong team before a run).
        plan_score = await self._debias_score(
            objective.plan_rubric, json.dumps(plan.manifest), f"{objective.id}#plan/{index}"
        )
        if plan_score.judge_unavailable or not plan_score.passed:
            # a degraded panel is NOT a pass and NOT a run-trigger — never spend a run on it.
            return SampleVerdict(index=index, passed=False, plan=plan_score)
        # run-outcome: only if the objective declares one AND a run callback is wired.
        run_score: DebiasedScore | None = None
        if objective.run_rubric is not None and self._run is not None:
            try:
                deliverable = await self._run(plan.manifest)
            except Exception as exc:  # noqa: BLE001 — run/infra failure → an errored sample
                return SampleVerdict(
                    index=index,
                    passed=False,
                    errored=True,
                    error_reason=type(exc).__name__,
                    plan=plan_score,
                )
            run_score = await self._debias_score(
                objective.run_rubric, deliverable, f"{objective.id}#run/{index}"
            )
        passed = plan_score.passed and (
            run_score is None or (run_score.passed and not run_score.judge_unavailable)
        )
        return SampleVerdict(index=index, passed=passed, plan=plan_score, run=run_score)

    async def run_objective(self, objective: Objective) -> ShipBarVerdict:
        bar = self._manifest.ship_bar
        samples = await asyncio.gather(
            *(self._score_sample(objective, i) for i in range(bar.n_samples))
        )
        pass_count = sum(1 for s in samples if s.passed)
        consensus_ratio = pass_count / len(samples)
        median_score = statistics.median([s.score for s in samples])
        # the worst judge-disagreement across the samples' scored stages (guardrail-blocked → none).
        scored = [s.run if s.run is not None else s.plan for s in samples]
        present = [d for d in scored if d is not None]
        variance = max((d.variance for d in present), default=0.0)
        # samples we could NOT confidently evaluate (an infra error or a judge outage) — these never
        # let an objective SHIP, and they make the call inconclusive, not a confident verdict.
        unevaluable = sum(1 for s in samples if s.unevaluable)
        passed = (
            pass_count >= bar.k_pass
            and median_score >= bar.min_score
            and variance <= bar.max_variance
            and unevaluable == 0
        )
        if passed:
            recommendation: Literal["ship", "revise", "escalate", "inconclusive"] = "ship"
        elif unevaluable > 0:
            recommendation = "inconclusive"  # a compile/run error or a judge outage — not a verdict
        elif variance > bar.max_variance:
            recommendation = "inconclusive"  # the panel disagreed too much to call it
        elif pass_count == 0:
            recommendation = "escalate"
        else:
            recommendation = "revise"
        return ShipBarVerdict(
            objective_id=objective.id,
            samples=list(samples),
            pass_count=pass_count,
            consensus_ratio=round(consensus_ratio, 4),
            median_score=round(median_score, 4),
            variance=round(variance, 4),
            passed=passed,
            recommendation=recommendation,
        )

    async def run(self) -> EvalSetResult:
        results = [await self.run_objective(o) for o in self._manifest.objectives]
        passed_objectives = sum(1 for r in results if r.passed)
        ratio = passed_objectives / len(results) if results else 0.0
        overall = ratio >= self._manifest.ship_bar.objective_pass_ratio
        return EvalSetResult(
            name=self._manifest.name,
            objectives=results,
            overall_passed=overall,
            summary={
                "objectives": len(results),
                "objectives_passed": passed_objectives,
                "objective_pass_ratio": round(ratio, 4),
                "ship_bar": self._manifest.ship_bar.model_dump(),
                "samples_per_objective": self._manifest.ship_bar.n_samples,
                "judges": len(self._judges),
            },
        )
