"""oraclous-eval — the rubric evaluator + judge + types (ADR-037 / E4 #469). No network: a fake
judge returns canned JSON per criterion."""

from __future__ import annotations

import asyncio

import pytest
from oraclous_eval import (
    Dimension,
    Evaluated,
    EvaluationCapacityExceeded,
    JudgeConfig,
    OpenAIEvalJudge,
    Rubric,
    RubricEvaluator,
    Verdict,
    make_judge,
)

pytestmark = [pytest.mark.unit]

_EVALUATED = Evaluated(
    target_kind="member_output", target_ref="run-1/member-a", organisation_id="org-a"
)


class _FakeJudge:
    def __init__(
        self,
        *,
        responses: dict[str, str] | None = None,
        raise_on: str | None = None,
        sleep: float = 0.0,
        default: str = '{"score": 0.9, "reason": "ok"}',
    ) -> None:
        self._responses = responses or {}
        self._raise_on = raise_on
        self._sleep = sleep
        self._default = default

    async def complete_json(self, *, system: str, user: str) -> str:
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raise_on and self._raise_on in user:
            raise RuntimeError("judge down")
        for key, resp in self._responses.items():
            if key in user:
                return resp
        return self._default

    async def complete_text(self, *, system: str, user: str) -> str:
        return "text"


def _rubric(*dims: Dimension, pass_threshold: float = 0.7) -> Rubric:
    return Rubric(dimensions=list(dims), pass_threshold=pass_threshold)


async def _run(judge: _FakeJudge, rubric: Rubric, **kw) -> Verdict:
    ev = RubricEvaluator(judge, **kw)
    return await ev.evaluate(rubric=rubric, target_output="the output", evaluated=_EVALUATED)


async def test_passing_verdict() -> None:
    v = await _run(
        _FakeJudge(default='{"score": 0.9, "reason": "good"}'),
        _rubric(Dimension(name="quality", prompt="is it good")),
    )
    assert v.passed is True and v.score == 0.9 and v.recommended_action == "accept"
    assert v.dimension_scores == {"quality": 0.9} and v.failures == []
    assert v.metrics_computed == ["quality"]
    assert v.model_dump(by_alias=True)["pass"] is True  # the `pass` JSON alias


async def test_below_threshold_no_critical_revises() -> None:
    v = await _run(
        _FakeJudge(default='{"score": 0.3}'),
        _rubric(Dimension(name="depth", prompt="deep", threshold=0.5, severity="major")),
    )
    assert v.passed is False and v.recommended_action == "revise"
    assert v.failures[0].dimension == "depth" and v.failures[0].score == 0.3


async def test_critical_failure_escalates_human() -> None:
    v = await _run(
        _FakeJudge(default='{"score": 0.2}'),
        _rubric(Dimension(name="integrity", prompt="cited", threshold=0.6, severity="critical")),
    )
    assert v.passed is False and v.recommended_action == "escalate_human"
    assert any(f.severity == "critical" for f in v.failures)


async def test_fail_soft_on_judge_error() -> None:
    v = await _run(
        _FakeJudge(raise_on="output"),  # every call raises
        _rubric(Dimension(name="d", prompt="x")),
    )
    assert v.passed is False and v.score == 0.0  # nothing computed → fail-closed 0
    assert v.dimension_scores == {} and v.failures and v.warnings


@pytest.mark.parametrize("bad", ['{"score": 1.5}', '{"score": "high"}', "not json", "{}"])
async def test_strict_parse_fail_soft(bad: str) -> None:
    v = await _run(_FakeJudge(default=bad), _rubric(Dimension(name="d", prompt="x")))
    assert v.dimension_scores == {} and v.failures[0].reason == "uncomputable"


async def test_weighted_score() -> None:
    judge = _FakeJudge(responses={"A": '{"score": 0.8}', "B": '{"score": 0.4}'})
    v = await _run(
        judge,
        _rubric(
            Dimension(name="a", prompt="A", weight=1.0, threshold=0.0),
            Dimension(name="b", prompt="B", weight=3.0, threshold=0.0),
        ),
    )
    assert v.score == 0.5  # (0.8*1 + 0.4*3) / 4


async def test_deterministic_dimension_via_resolver() -> None:
    ev = RubricEvaluator(_FakeJudge())

    async def resolver(dim: Dimension, target: str) -> float:
        return 1.0 if "marker" in target else 0.0

    v = await ev.evaluate(
        rubric=_rubric(
            Dimension(
                name="has-marker", prompt="marker present", kind="deterministic", threshold=0.5
            )
        ),
        target_output="contains marker here",
        evaluated=_EVALUATED,
        deterministic_resolver=resolver,
    )
    assert v.dimension_scores == {"has-marker": 1.0} and v.passed is True


async def test_deterministic_without_resolver_fail_soft() -> None:
    v = await _run(_FakeJudge(), _rubric(Dimension(name="d", prompt="x", kind="deterministic")))
    assert v.dimension_scores == {} and v.failures  # no resolver → fail-soft null


async def test_deadline_returns_partial() -> None:
    v = await _run(
        _FakeJudge(sleep=0.3), _rubric(Dimension(name="slow", prompt="x")), deadline_seconds=0.05
    )
    assert v.dimension_scores == {} and any("deadline" in w for w in v.warnings)
    assert v.failures[0].reason == "deadline"


async def test_capacity_exceeded_when_slots_locked() -> None:
    slots = asyncio.Semaphore(1)
    await slots.acquire()  # exhaust
    ev = RubricEvaluator(_FakeJudge(), slots=slots)
    with pytest.raises(EvaluationCapacityExceeded):
        await ev.evaluate(
            rubric=_rubric(Dimension(name="d", prompt="x")), target_output="o", evaluated=_EVALUATED
        )


def test_make_judge_none_without_key() -> None:
    assert make_judge(JudgeConfig(api_key=None)) is None


def test_make_judge_builds_with_key() -> None:
    judge = make_judge(JudgeConfig(api_key="sk-test", model_name="openai/gpt-4o-mini"))
    assert isinstance(judge, OpenAIEvalJudge)
