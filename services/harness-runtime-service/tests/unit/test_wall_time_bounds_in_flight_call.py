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
# a constant max_wall_time_seconds" implementation cannot see, because 2.5 < 3 lets it complete
# unchecked, silently defeating the fix once a run has more than one iteration.
_SECOND_CALL_SECONDS = 2.5


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


# ── be-test-reviewer (PR #1070, FIX 2): a per-call bound must not silently defeat the EXISTING
# transient-error retry (ADR-042 #551). tool_use.py:68 allows _LLM_MAX_RETRIES retries (default 4)
# and :1894 already refuses to retry past the run's own wall-time budget; if the per-call bound
# this issue adds is derived carelessly (e.g. dividing the budget by the retry count, or reusing a
# single deadline that does not account for how many attempts are still owed), a real transient
# provider error (rate-limit / 5xx) can stop being recoverable even though the run's OVERALL budget
# would easily have covered every attempt. This is a regression GUARD, not a pin on a defect that
# exists today (no per-call bound exists yet to interact with retries, so this currently PASSES) —
# it exists so the fix for items 1/2 cannot ship a per-call bound that quietly breaks retries. ────


class _FlakyThenSucceedsLLM:
    """Raises a TRANSIENT error ``fail_n`` times, each attempt taking ``call_seconds`` (models real
    network latency per attempt), then answers — mirrors test_tool_use_loop.py's ``_FlakyLLM`` but
    with a non-zero per-call duration so a per-call bound has something to actually interact with.
    """

    protocol_shape = "fake"

    def __init__(self, *, fail_n: int, call_seconds: float) -> None:
        self.calls = 0
        self._fail_n = fail_n
        self._call_seconds = call_seconds

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.calls += 1
        await asyncio.sleep(self._call_seconds)
        if self.calls <= self._fail_n:
            raise LLMClientError("LLM call → 429: rate limited", status_code=429, transient=True)
        return LLMResponse(text="done")


async def test_bounded_retries_still_get_to_run_under_a_per_call_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-call wall-clock bound must leave room for the FULL retry budget when the run's own
    overall wall-time budget can plainly afford it. Exhausts every retry slot
    (``tool_use._LLM_MAX_RETRIES``) and succeeds only on the very last allowed attempt — asserting
    the BEHAVIOUR (every attempt gets to run, and the call eventually succeeds), never a specific
    constant or a specific derivation of the per-call bound.
    """
    monkeypatch.setattr(tool_use, "_async_sleep", _no_sleep)  # deterministic + fast backoff

    fail_n = tool_use._LLM_MAX_RETRIES  # use every retry slot, then clear on the LAST attempt
    per_call_seconds = 0.05
    total_attempts = fail_n + 1
    # A generous margin: even with a per-call bound in play, `total_attempts` attempts at
    # `per_call_seconds` each must comfortably fit inside the run's own overall budget.
    max_wall_seconds = int(per_call_seconds * total_attempts * 10) + 1

    llm = _FlakyThenSucceedsLLM(fail_n=fail_n, call_seconds=per_call_seconds)
    policy = PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=max_wall_seconds,
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
        f"expected every retry slot to run ({total_attempts} attempts total: {fail_n} transient "
        f"failures + 1 success), got {llm.calls} — a per-call bound derived without regard for how "
        "many attempts are still owed can starve the existing transient-error retry."
    )
