"""Regression guard (security review, PR #1071): a run cut off mid-call by its OWN wall-time
budget must report the CODED ``wall_time`` terminal, never a bare exception class name.

``tool_use.py``'s single ``_complete_with_retry`` call site does::

    except _WallTimeBudgetExhausted:
        return _budget_gate("budget", "wall_time", ...)
    except Exception as exc:  # noqa: BLE001
        ...  # FAILED, error_type=type(exc).__name__

``_WallTimeBudgetExhausted`` is itself an ``Exception`` subclass, so the ORDER of those two clauses
is load-bearing: swap them and the generic handler catches it first, and a person reads the
internal signal exception's own class name as the reason instead of a sentence naming a time
limit. This is a one-line mistake away from recurring, so it is pinned directly rather than relied
on only via the elapsed-time assertions in ``test_wall_time_bounds_in_flight_call.py``.
"""

from __future__ import annotations

import asyncio
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

# The run's own wall-clock budget — small so the test is fast.
_MAX_WALL_SECONDS = 1
# The fake LLM's single call — many times the budget, so the cutoff is unambiguously the budget
# firing mid-call, not the call happening to finish just under it.
_CALL_DURATION_SECONDS = 6.0


class _SlowLLM:
    protocol_shape = "fake"

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        await asyncio.sleep(_CALL_DURATION_SECONDS)
        return LLMResponse(text="finally answered")


async def _ok_dispatch(spec: ToolSpec, args: dict) -> dict:
    return {"tables": []}


async def test_a_wall_time_cutoff_never_reports_a_bare_exception_class_name() -> None:
    policy = PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=_MAX_WALL_SECONDS,
        max_tokens=None,
    )
    result = await run_tool_use_loop(
        llm=_SlowLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=policy,
    )
    assert result.status is HarnessStatus.ESCALATED, result
    assert result.error_type == "wall_time", result
    # the specific regression this guards: the internal signal exception's own class name must
    # never surface as the reason, on either field the run page might render.
    assert "WallTimeBudgetExhausted" not in (result.error_message or ""), result
    assert "WallTimeBudgetExhausted" not in (result.error_type or ""), result
    assert result.error_message is not None
    assert "wall" in result.error_message.lower()
