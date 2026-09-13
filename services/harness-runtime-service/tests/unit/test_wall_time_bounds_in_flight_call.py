"""#1067 (R1, item 2) — the run's OWN wall-clock budget must bound an IN-FLIGHT model call, not only
the gaps between iterations.

``run_tool_use_loop``'s ``_over_wall_time()`` (tool_use.py:1321) is evaluated at the top of each
iteration (:1463/:1894/:1914) — never WHILE ``await llm.complete(...)`` is running
(``_complete_with_retry``, :1882-1902, just awaits it directly). So one slow call runs straight
through ``policy.max_wall_time_seconds`` no matter how small it is: the loop only notices the
overrun after the call finally returns.

This test uses a fake LLM whose ``complete()`` sleeps far longer than the policy's wall-time budget,
and asserts the loop returns its budget-exhausted terminal (``_budget_gate("budget", "wall_time",
...)`` -> ``HarnessStatus.ESCALATED`` with ``error_type == "wall_time"``) at/near the budget — not
after the slow call completes. The elapsed-time assertion uses a generous relative tolerance against
the call's own duration (never a hand-derived exact number): today the loop can only return AFTER
the full call duration, which this test picks large enough that "near the budget" and "after the
full call" are unmistakably different.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.llm.openai_compatible import LLMClientError
from oraclous_harness_runtime_service.domain.loop import tool_use
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]


async def _no_sleep(_seconds: float) -> None:
    """A no-op stand-in for asyncio.sleep so retry backoff is deterministic + fast (matches the
    pattern in test_tool_use_loop.py's ADR-042 retry tests)."""


_SPEC = ToolSpec(
    name="pg__list_tables",
    description="list tables",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="pg",
    operation="list_tables",
)

# The policy's own wall-clock budget for this run.
_MAX_WALL_SECONDS = 1
# How long the fake LLM's single call takes — many times the budget, so "returned near the budget"
# and "returned only after the call finished" can never be confused for one another.
_CALL_DURATION_SECONDS = 6.0


async def _ok_dispatch(spec: ToolSpec, args: dict) -> dict:
    return {"tables": ["a", "b"]}


class _SlowLLM:
    """Answers, eventually — but takes far longer than the run's own wall-clock budget."""

    protocol_shape = "fake"

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.call_count += 1
        await asyncio.sleep(_CALL_DURATION_SECONDS)
        return LLMResponse(text="finally answered")


async def test_in_flight_call_is_bounded_by_the_runs_own_wall_time_budget() -> None:
    llm = _SlowLLM()
    policy = PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=_MAX_WALL_SECONDS,
        max_tokens=None,
    )
    started = time.monotonic()
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=policy,
    )
    elapsed = time.monotonic() - started

    assert result.status is HarnessStatus.ESCALATED, result
    assert result.error_type == "wall_time", result

    # The budget is bounded WHILE the call is in flight: elapsed must land far closer to the
    # configured budget than to the full (much longer) call duration. A generous relative
    # tolerance — never a hand-derived exact number — against the call's own duration.
    assert elapsed < _CALL_DURATION_SECONDS / 2, (
        f"the loop returned after {elapsed:.2f}s against a wall-time budget of "
        f"{_MAX_WALL_SECONDS}s and a call that takes {_CALL_DURATION_SECONDS}s — it waited for the "
        "in-flight call to finish before ever checking the budget again, instead of bounding the "
        "call itself"
    )


# ── be-test-reviewer (PR #1070): the single-iteration scenario above cannot tell a CORRECT bound
# (the call is cut off at what actually REMAINS of the budget) apart from a WRONG one (every
# in-flight call gets a fresh copy of the full max_wall_time_seconds window). At iteration 1 the
# time already spent is ~0, so "remaining" and "the full window" are the same number. A
# multi-iteration scenario is needed where they visibly differ. ──────────────────────────────────

# The run's total wall-clock budget.
_MULTI_MAX_WALL_SECONDS = 3
# Iteration 1's tool dispatch alone burns most of the budget, so by the time iteration 2's model
# call begins, only a SMALL fraction of the budget remains.
_FIRST_DISPATCH_SECONDS = 2.0
# Iteration 2's model call takes longer than what's actually LEFT of the budget (1s) but
# comfortably less than a FRESH copy of the full window (3s) — the exact gap a "bound every call at
# a constant max_wall_time_seconds" implementation cannot see, because 3.0 < the full window lets
# it complete unchecked, silently defeating the fix once a run has more than one iteration.
#
# Test-quality review (PR #1071): raised from 2.5s so the CORRECT-run threshold below carries a
# third of headroom instead of a quarter — the prior margin (25%) was measured rock-solid on a
# twelve-core machine, but a two-core CI runner's scheduling jitter is a different environment.
_SECOND_CALL_SECONDS = 3.0


async def _slow_tool_dispatch(spec: ToolSpec, args: dict) -> dict:
    await asyncio.sleep(_FIRST_DISPATCH_SECONDS)
    return {"tables": ["a", "b"]}


class _BudgetBurningThenSlowLLM:
    """Answers the FIRST turn with a tool call (fast) — the tool DISPATCH is what burns the
    budget, not this call. The SECOND turn's own call is what must be bounded by what's left."""

    protocol_shape = "fake"

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.call_count += 1
        if self.call_count == 1:
            return LLMResponse(text="", tool_calls=[ToolCall("c1", tools[0].name, {})])
        await asyncio.sleep(_SECOND_CALL_SECONDS)
        return LLMResponse(text="finally answered")


async def test_in_flight_call_is_bounded_by_the_remaining_budget_not_a_fresh_window() -> None:
    """#1067 (R1, item 2) — be-test-reviewer finding on PR #1070: a per-call bound must shrink as
    the run spends its budget across EARLIER iterations. An implementation that instead re-arms a
    fresh ``max_wall_time_seconds`` window on every call is indistinguishable from a correct one on
    a single-iteration run (both start with the full budget still remaining) — it only shows up once
    a run has already spent time before a later call begins, which is exactly what this test forces.
    """
    llm = _BudgetBurningThenSlowLLM()
    policy = PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=_MULTI_MAX_WALL_SECONDS,
        max_tokens=None,
    )
    started = time.monotonic()
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_slow_tool_dispatch,
        policy=policy,
    )
    elapsed = time.monotonic() - started

    assert result.status is HarnessStatus.ESCALATED, result
    assert result.error_type == "wall_time", result

    # Two candidate outcomes, both derived from this test's own constants (never hand-typed):
    #   - CORRECT: the second call is cut off at what actually REMAINS of the budget, landing total
    #     elapsed near the run's full budget.
    #   - WRONG: the second call is (re-)bounded at a fresh full window each time, so it runs to
    #     completion unchecked, landing total elapsed near dispatch-time + the full second call.
    # The threshold is the midpoint between the two — a generous margin computed from the test's own
    # numbers, not a hand-derived constant.
    elapsed_if_correct = _MULTI_MAX_WALL_SECONDS
    elapsed_if_bounded_by_a_fresh_window_each_call = _FIRST_DISPATCH_SECONDS + _SECOND_CALL_SECONDS
    threshold = (elapsed_if_correct + elapsed_if_bounded_by_a_fresh_window_each_call) / 2
    assert elapsed < threshold, (
        f"the loop returned after {elapsed:.2f}s — closer to bounding the second call at a "
        f"FRESH {_MULTI_MAX_WALL_SECONDS}s window "
        f"({elapsed_if_bounded_by_a_fresh_window_each_call:.2f}s total) than at what actually "
        f"REMAINED of the budget ({elapsed_if_correct:.2f}s total). An implementation that resets "
        "the per-call bound to the full window on every iteration lets a multi-iteration run "
        "overrun its own budget by up to a full window per iteration."
    )
    # sanity: the loop cannot possibly return before the time already spent on the first dispatch.
    assert elapsed > _FIRST_DISPATCH_SECONDS


# ── be-test-reviewer (PR #1070, FIX 2, round 2): a per-call bound must not silently defeat the
# EXISTING transient-error retry (ADR-042 #551). tool_use.py:68 allows _LLM_MAX_RETRIES retries
# (default 4) and :1894 already refuses to retry past the run's own wall-time budget; if the
# per-call bound this issue adds is derived carelessly (e.g. dividing the budget EQUALLY across
# every attempt, ignoring how much of it earlier attempts already spent), a real transient provider
# error (rate-limit / 5xx) can stop being recoverable even though the run's OVERALL budget would
# easily have covered every attempt.
#
# Round 1 gave every attempt the SAME duration ``d`` — mathematically impossible as a discriminator:
# for N attempts of equal duration, catching a careless ``budget / N`` per-call bound needs
# ``d > budget / N``, while a CORRECT implementation completing all N attempts needs
# ``N * d <= budget``. No choice of ``d`` satisfies both at once, so nothing could ever go RED under
# the careless implementation this test exists to catch.
#
# The fix is ASYMMETRY, which also matches reality: a rate-limit/5xx rejection comes back almost
# instantly; a real completion is the slow part. The failing attempts are near-instant; ONLY the
# final, successful attempt is slow. Keep this asymmetry EXPLICIT — an "obvious" refactor back to
# uniform durations would silently make the test unable to catch anything again. Self-checked below
# (``assert careless_equal_division_bound < _FINAL_CALL_SECONDS`` etc.) so that regression is loud,
# not silent, if the constants ever drift. ─────────────────────────────────────────────────────────

# A rate-limit/5xx rejection: near-instant.
_FAILING_CALL_SECONDS = 0.01
# A real completion: the slow part, by design.
_FINAL_CALL_SECONDS = 2.0
# The run's own wall-clock budget — deliberately sized so the two candidate per-call bounds
# (checked below) land on opposite sides of `_FINAL_CALL_SECONDS`.
_RUN_BUDGET_SECONDS = 3


class _FlakyThenSucceedsLLM:
    """Fails ``fail_n`` times near-instantly (``_FAILING_CALL_SECONDS`` each — a rejection bounces
    back fast), then takes ``_FINAL_CALL_SECONDS`` to actually answer (a real completion is slow).
    Mirrors test_tool_use_loop.py's ``_FlakyLLM``, but with the asymmetric durations FIX 2 needs.
    """

    protocol_shape = "fake"

    def __init__(self, *, fail_n: int) -> None:
        self.calls = 0
        self._fail_n = fail_n

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.calls += 1
        if self.calls <= self._fail_n:
            await asyncio.sleep(_FAILING_CALL_SECONDS)
            raise LLMClientError("LLM call → 429: rate limited", status_code=429, transient=True)
        await asyncio.sleep(_FINAL_CALL_SECONDS)
        return LLMResponse(text="done")


async def test_bounded_retries_still_get_to_run_under_a_per_call_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-call wall-clock bound must leave room for the FULL retry budget when the run's own
    overall wall-time budget can plainly afford it. Exhausts every retry slot
    (``tool_use._LLM_MAX_RETRIES``) with near-instant transient failures, then succeeds on the very
    last allowed attempt — which is the SLOW one. Asserts the BEHAVIOUR (every attempt gets to run,
    and the call eventually succeeds), never a specific constant or a specific derivation of the
    per-call bound.

    The asymmetric durations make two candidate per-call bounds disagree:

    - a CARELESS one that divides the budget EQUALLY across every attempt
      (``_RUN_BUDGET_SECONDS / total_attempts``) lands well UNDER ``_FINAL_CALL_SECONDS`` — it would
      cut off the slow final call, and the run would never reach SUCCEEDED.
    - a CORRECT one that bounds each call at what actually REMAINS lands, by the time the final call
      starts, at ``_RUN_BUDGET_SECONDS`` minus the (near-instant) failing attempts already spent —
      comfortably OVER ``_FINAL_CALL_SECONDS`` — so the final call is left alone and succeeds.

    This test currently PASSES: no per-call bound exists yet (item 1/2's own gap), so nothing bounds
    ANY call today and the final attempt simply gets to run. It exists to catch, once a per-call
    bound is wired in, the SPECIFIC careless mistake of deriving it as ``budget / attempt_count``
    (or any other bound that ignores how much of the budget earlier attempts already spent) rather
    than off the time that actually REMAINS.
    """
    monkeypatch.setattr(tool_use, "_async_sleep", _no_sleep)  # deterministic + fast backoff

    fail_n = tool_use._LLM_MAX_RETRIES  # exhaust every retry slot, then clear on the LAST attempt
    total_attempts = fail_n + 1

    # Self-check: confirm the chosen constants actually discriminate the two implementations, so a
    # future edit (e.g. "simplifying" the durations back to uniform, or drifting the budget) cannot
    # silently turn this back into an impossible — always-passing-for-the-wrong-reason — test.
    careless_equal_division_bound = _RUN_BUDGET_SECONDS / total_attempts
    correct_remaining_time_at_final_call = _RUN_BUDGET_SECONDS - fail_n * _FAILING_CALL_SECONDS
    assert careless_equal_division_bound < _FINAL_CALL_SECONDS, (
        f"scenario no longer discriminates: an equal-division bound "
        f"({careless_equal_division_bound}s) would not cut off the {_FINAL_CALL_SECONDS}s final "
        "call, so this test could never catch that mistake"
    )
    assert correct_remaining_time_at_final_call > _FINAL_CALL_SECONDS, (
        f"scenario no longer discriminates: even a CORRECT remaining-time bound "
        f"({correct_remaining_time_at_final_call}s) would cut off the {_FINAL_CALL_SECONDS}s final "
        "call, so a correct implementation would fail this test too"
    )

    llm = _FlakyThenSucceedsLLM(fail_n=fail_n)
    policy = PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=_RUN_BUDGET_SECONDS,
        max_tokens=None,
    )
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=policy,
    )
    assert result.status is HarnessStatus.SUCCEEDED, result
    assert llm.calls == total_attempts, (
        f"expected every retry slot to run ({total_attempts} attempts total: {fail_n} "
        f"near-instant transient failures + 1 slow success), got {llm.calls} — a per-call bound "
        "that ignores how much of the budget the earlier (fast) failures already spent can "
        "starve the slow final attempt that would otherwise have succeeded."
    )
