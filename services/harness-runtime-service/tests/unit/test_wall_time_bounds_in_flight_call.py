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
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]

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
