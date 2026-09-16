"""#1072 (T1) — the tool-use loop must expose live progress to a caller that runs it as a
cancellable ``asyncio`` task, and must never swallow ``asyncio.CancelledError``.

The service-level design (ADR: engine dispatches a cancel via a Postgres lease + watcher, #1072
design doc) needs the loop's spend to survive a cancellation that lands mid-LLM-call: the service
persists a ``CANCELLED`` terminal from whatever the loop already booked, not from nothing. That
requires a shared MUTABLE object the loop writes into AS IT GOES (token counts, steps) rather than
only handing them back in the ``LoopResult`` it returns on completion — a cancelled task's coroutine
never reaches its ``return``, so anything only stored in local variables is lost.

**Not-yet-built seam.** This test exercises ``LoopProgress`` — a small mutable dataclass that does
not exist yet. The implementer should add it at
``oraclous_harness_runtime_service.domain.loop.progress`` (new module,
``services/harness-runtime-service/src/oraclous_harness_runtime_service/domain/loop/progress.py``)
with at least the fields this test reads: ``total_tokens: int``, ``prompt_tokens: int``,
``completion_tokens: int``, and ``steps: list[LoopStep]``. ``run_tool_use_loop`` (`domain/loop/
tool_use.py`) should grow a new keyword-only parameter, ``progress: LoopProgress | None = None``
(default ``None`` so every existing caller is unaffected), and update it after each LLM response and
each step append — synchronously, in the same task, so a cancellation that arrives right after does
not lose what was already booked.

Per ADR-010 / `.claude/rules/tests-seam-imports.md`, ``LoopProgress`` is imported function-locally
inside each test (not at module level): today it does not exist, so importing it hard-fails the
test at runtime with ``ModuleNotFoundError`` — RED for the right reason — without breaking
collection for the rest of the suite. ``run_tool_use_loop`` already exists; passing it an
unrecognised ``progress=`` keyword before the seam lands raises ``TypeError`` instead, which is an
equally valid, equally missing-seam RED reason once the import itself is fixed up.

Both tests here drive the loop with a fake LLM (the pattern used throughout
``test_wall_time_bounds_in_flight_call.py``): the first call answers fast with a tool call, the
second call sleeps far longer than the test will wait, standing in for a real in-flight HTTP call to
a model provider. The test cancels the loop's own ``asyncio.Task`` while that second call is still
in flight and asserts on what happened, never on what it thinks *should* have been booked.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import StepKind

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]

_SPEC = ToolSpec(
    name="pg__list_tables",
    description="list tables",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="pg",
    operation="list_tables",
)

# How long the fake LLM's SECOND call sleeps for — many times longer than any test here will wait
# before cancelling, so "cancelled while in flight" can never be confused with "the call finished
# on its own".
_SECOND_CALL_SLEEP_SECONDS = 60.0

# How long a test waits for the fake LLM's second call to actually start before giving up — a CI
# scheduling hiccup, not the behaviour under test, so generous on purpose.
_WAIT_FOR_SECOND_CALL_SECONDS = 5.0


async def _ok_dispatch(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    return {"tables": ["a", "b"]}


class _FirstCallCountsThenHangsLLM:
    """Turn 1: answers with a tool call and a KNOWN, non-round token split (so the split can't be
    mistaken for an untouched default). Turn 2 (and any later turn): sets ``second_call_started``
    then sleeps far longer than the test will wait — standing in for an in-flight provider call the
    test cancels out from under."""

    protocol_shape = "fake"

    def __init__(self, second_call_started: asyncio.Event) -> None:
        self.call_count = 0
        self._second_call_started = second_call_started

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.call_count += 1
        if self.call_count == 1:
            return LLMResponse(
                text="",
                tool_calls=[ToolCall("c1", tools[0].name, {})],
                total_tokens=100,
                input_tokens=60,
                output_tokens=40,
            )
        self._second_call_started.set()
        await asyncio.sleep(_SECOND_CALL_SLEEP_SECONDS)
        return LLMResponse(text="too late — the test should have cancelled long before this")


def _policy() -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
    )


async def test_progress_tracks_tokens_before_cancel() -> None:
    """The loop must book turn 1's tokens + steps into the shared ``LoopProgress`` AS THEY HAPPEN —
    not only on return — so that when the task is cancelled during turn 2's in-flight call, the
    caller (the future cancellation service) can still see exactly what turn 1 already spent."""
    from oraclous_harness_runtime_service.domain.loop.progress import LoopProgress

    second_call_started = asyncio.Event()
    llm = _FirstCallCountsThenHangsLLM(second_call_started)
    progress = LoopProgress()

    task = asyncio.create_task(
        run_tool_use_loop(
            llm=llm,
            system="",
            user_input="go",
            tool_specs=[_SPEC],
            dispatch=_ok_dispatch,
            policy=_policy(),
            progress=progress,
        )
    )

    await asyncio.wait_for(second_call_started.wait(), timeout=_WAIT_FOR_SECOND_CALL_SECONDS)
    # The second call has started (and is now asleep for far longer than we'll wait) — cancel the
    # loop's own task while it is genuinely in flight.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Turn 1's usage was booked into the shared object as it happened, split honestly (never just
    # the total): the caller can price a cut-short run's confirmed spend without waiting for a
    # LoopResult that a cancelled task will never produce.
    assert progress.total_tokens == 100, progress
    assert progress.prompt_tokens == 60, progress
    assert progress.completion_tokens == 40, progress

    # Turn 1's own steps — the model's tool-call turn and the tool dispatch it drove — are on the
    # shared object too, in order, well before the task was ever cancelled.
    recorded = [(s.kind, s.name, s.status) for s in progress.steps]
    assert (StepKind.LLM, "primary", "tool_calls") in recorded, recorded
    assert (StepKind.TOOL, "pg.list_tables", "ok") in recorded, recorded
    llm_index = recorded.index((StepKind.LLM, "primary", "tool_calls"))
    tool_index = recorded.index((StepKind.TOOL, "pg.list_tables", "ok"))
    assert llm_index < tool_index, recorded

    # The fake LLM really was cancelled mid-sleep, not left running past the test: only the two
    # calls we drove (the fast turn-1 answer and the turn-2 call this test cut off) ever happened.
    assert llm.call_count == 2


async def test_loop_does_not_swallow_cancellation() -> None:
    """``asyncio.CancelledError`` is a ``BaseException`` in modern Python precisely so a task
    cancellation is never mistaken for an ordinary failure by an ``except Exception`` handler. The
    loop's own transient-LLM-error retry (ADR-042 #551, ``_complete_with_retry``) and its
    permanent-error path both catch ``Exception`` around the very ``await llm.complete(...)`` this
    test cancels — this test pins that a cancellation reaches the caller as a real
    ``CancelledError``, never reclassified into a returned ``LoopResult`` (e.g. ``FAILED`` with
    ``error_type == "CancelledError"``), and that wiring ``progress=`` into that same code path does
    not change this."""
    from oraclous_harness_runtime_service.domain.loop.progress import LoopProgress

    second_call_started = asyncio.Event()
    llm = _FirstCallCountsThenHangsLLM(second_call_started)
    progress = LoopProgress()

    task = asyncio.create_task(
        run_tool_use_loop(
            llm=llm,
            system="",
            user_input="go",
            tool_specs=[_SPEC],
            dispatch=_ok_dispatch,
            policy=_policy(),
            progress=progress,
        )
    )

    await asyncio.wait_for(second_call_started.wait(), timeout=_WAIT_FOR_SECOND_CALL_SECONDS)
    task.cancel()

    # The task must actually finish by raising CancelledError out of `await task` — never by
    # quietly returning a LoopResult. A LoopResult return (even one shaped like a failure) is the
    # exact swallowing this test exists to catch, so it is asserted as a hard failure, not folded
    # into the `pytest.raises` block.
    with pytest.raises(asyncio.CancelledError):
        result = await task
        pytest.fail(
            f"run_tool_use_loop swallowed the cancellation and returned normally instead of "
            f"letting CancelledError propagate: {result!r}"
        )

    assert task.cancelled()
