"""#834 (DESIGN §E) — extend the #946 T2 no-progress ledger to an identical (tool, arguments,
RESULT) triple, not only an identical (tool, arguments, ERROR) triple.

``manifest-validate`` (the compiler reviewer's repair tool) can return SUCCESSFULLY with
``would_block: true`` — an unchanged verdict, not an error — so today's ledger
(``_repeated_failures_from_transcript`` / the dispatch-gating check in
``domain/loop/tool_use.py``) never fires for it: a member can call it a third, fourth, fifth time
for a byte-identical answer until its own ``max_tool_calls`` budget is gone.

Same constant (``_REPEATED_FAILURE_MAX`` = 2 dispatches allowed before the third is refused), same
typed stop status (the refused call's step is recorded ``"repeated_failure"``, mirroring the error
path), same three axes (tool + arguments + this time the RESULT, not the error). This file is
modelled directly on ``test_repeated_tool_failure.py`` (the #946 T2 tests) — read it first — and
reuses its fixtures/helpers verbatim so the two ledgers are proven with the same rigor.

RED until the [impl] extends the ledger past the ``if not failed: continue`` early-return in
``_repeated_failures_from_transcript`` (and the equivalent live-dispatch recording) to also track a
successful (``status == "ok"``) call's content.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)
from oraclous_harness_runtime_service.domain.loop.tool_use import LoopResult, run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import StepKind

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]


_VALIDATE = ToolSpec(
    name="manifest_validate",
    description="validate a drafted team manifest",
    parameters={
        "type": "object",
        "properties": {"draft": {"type": "string"}},
        "required": [],
    },
    binding="compiler",
    operation="manifest-validate",
)
_SAME_ARGS = {"draft": "the same draft, byte for byte"}
_SAME_VERDICT = {"would_block": True, "blocking": ["F-NO-MEMBERS: the draft has no members[]"]}


def _env(*, max_iterations: int = 8) -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=max_iterations,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
        gated_bindings=frozenset(),
        tool_ceiling=frozenset(),
        redact_patterns=(),
    )


class _StubbornLLM:
    """Re-sends the identical call forever — the reviewer benignly re-validating past its cap."""

    protocol_shape = "fake"

    def __init__(self, args: dict | None = None, *, tool: str = _VALIDATE.name) -> None:
        self.args = dict(_SAME_ARGS) if args is None else args
        self.tool = tool
        self.turns = 0
        self.seen: list[Message] = []

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        self.turns += 1
        self.seen = [dict(m) for m in messages]
        return LLMResponse(
            text="", tool_calls=[ToolCall(f"c{self.turns}", self.tool, dict(self.args))]
        )


class _OutcomeDispatcher:
    """Records every call it was actually asked to make. Plays back a fixed sequence of outcomes,
    each either ``("ok", result_dict)`` or ``("error", message)`` — the last outcome repeats once
    the sequence is exhausted, mirroring ``_Dispatcher.errors`` in the #946 T2 file."""

    def __init__(self, outcomes: list[tuple[str, Any]]) -> None:
        self.calls: list[dict] = []
        self.outcomes = outcomes

    async def __call__(self, spec: ToolSpec, args: dict) -> dict:
        self.calls.append(dict(args))
        index = min(len(self.calls) - 1, len(self.outcomes) - 1)
        kind, payload = self.outcomes[index]
        if kind == "error":
            raise RuntimeError(payload)
        return payload


def _tool_steps(result: LoopResult) -> list:
    return [s for s in result.steps if s.kind is StepKind.TOOL]


def _tool_replies(llm: _StubbornLLM) -> list[Message]:
    return [m for m in llm.seen if m.get("role") == "tool"]


# --- the bound itself, on the RESULT axis -------------------------------------------------------


async def test_same_call_returning_identical_result_twice_not_dispatched_a_third_time() -> None:
    dispatch = _OutcomeDispatcher([("ok", dict(_SAME_VERDICT))])
    await run_tool_use_loop(
        llm=_StubbornLLM(),
        system="",
        user_input="go",
        tool_specs=[_VALIDATE],
        dispatch=dispatch,
        policy=_env(max_iterations=8),
    )
    # exactly two, not "at most two" — the second dispatch is the deliberate allowance for a
    # would-be-transient repeat, matching the #946 T2 error-axis bound exactly.
    assert len(dispatch.calls) == 2


async def test_the_third_identical_result_is_refused_and_recorded_repeated_failure() -> None:
    dispatch = _OutcomeDispatcher([("ok", dict(_SAME_VERDICT))])
    result = await run_tool_use_loop(
        llm=_StubbornLLM(),
        system="",
        user_input="go",
        tool_specs=[_VALIDATE],
        dispatch=dispatch,
        policy=_env(max_iterations=8),
    )
    steps = _tool_steps(result)
    refused = [s for s in steps if s.status == "repeated_failure"]
    # the SAME typed stop status as the error-axis bound (DESIGN §E: "same typed stop reason") —
    # this is not a new, second mechanism.
    assert refused, "expected at least one step refused as 'repeated_failure' on the RESULT axis"


async def test_the_refusal_still_answers_the_model_not_only_the_operator_trace() -> None:
    # #946 T2's own invariant, unchanged: a provider rejects a tool_call with no answering message,
    # so a refused-for-repetition call must still get a tool-role reply.
    #
    # Correction (three independent reviewers on PR #1017, reproduced against unmodified code):
    # the original `len(replies) == llm.turns` was off by one BY CONSTRUCTION, not a loop bug.
    # `_StubbornLLM.seen` snapshots `messages` at the START of `complete()`, before that same
    # turn's own reply is appended — so the loop's very last turn's own reply is never observable
    # through this stub, at any iteration budget (verified exactly at 3, 5, 8, 12). Rather than pin
    # `llm.turns - 1` (still coupled to the stub's snapshot timing), assert the actual PROPERTY
    # this test is named for directly: every tool_call the model issued has a matching answering
    # reply. This is a set-subset check, not a count, so it holds regardless of when the stub last
    # snapshotted — it mirrors `test_repeated_tool_failure.py`'s own
    # `test_every_call_the_member_made_has_an_answering_message` (the #946 T2 precedent this file
    # is modelled on), which asserts the same invariant the same way for exactly this reason.
    llm = _StubbornLLM()
    await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_VALIDATE],
        dispatch=_OutcomeDispatcher([("ok", dict(_SAME_VERDICT))]),
        policy=_env(max_iterations=8),
    )
    called = {
        call["id"]
        for message in llm.seen
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    }
    answered = {m.get("tool_call_id") for m in _tool_replies(llm)}
    assert called and called <= answered


async def test_a_call_returning_a_different_result_each_time_is_always_dispatched() -> None:
    # the RESULT axis of the triple: a genuinely changing answer (the reviewer's repeated
    # re-validation of an EVOLVING draft, not a stuck one) must never be refused.
    dispatch = _OutcomeDispatcher(
        [
            ("ok", {"would_block": True, "blocking": ["F-NO-MEMBERS"]}),
            ("ok", {"would_block": True, "blocking": ["F-DRAFT-INVALID: members.0.role missing"]}),
            ("ok", {"would_block": True, "blocking": ["F-CAPABILITY-MISSING: 'teleport'"]}),
            ("ok", {"would_block": False, "blocking": []}),
        ]
    )
    await run_tool_use_loop(
        llm=_StubbornLLM(),
        system="",
        user_input="go",
        tool_specs=[_VALIDATE],
        dispatch=dispatch,
        policy=_env(max_iterations=4),
    )
    assert len(dispatch.calls) == 4  # every distinct verdict dispatched — the bound never fired


# --- cross-outcome independence: pinning that the EXISTING failure path is unaffected -----------


async def test_a_failure_does_not_pre_spend_an_identical_successful_results_allowance() -> None:
    """A failure is a DIFFERENT outcome from an identical successful result that follows it — it
    must not count toward the RESULT-axis allowance. Two identical successes are still allowed
    after one earlier, unrelated failure; only the third identical success is refused."""
    dispatch = _OutcomeDispatcher(
        [
            ("error", "the registry was momentarily unreachable"),
            ("ok", dict(_SAME_VERDICT)),
            ("ok", dict(_SAME_VERDICT)),
            ("ok", dict(_SAME_VERDICT)),
            ("ok", dict(_SAME_VERDICT)),
        ]
    )
    await run_tool_use_loop(
        llm=_StubbornLLM(),
        system="",
        user_input="go",
        tool_specs=[_VALIDATE],
        dispatch=dispatch,
        policy=_env(max_iterations=8),
    )
    # 1 failure (always dispatched, it's the first of its kind) + 2 allowed identical successes;
    # the 3rd identical success is refused.
    assert len(dispatch.calls) == 3


async def test_the_existing_error_axis_bound_is_unchanged_by_this_extension() -> None:
    # verbatim regression of test_repeated_tool_failure.py's core assertion — the RESULT-axis
    # extension must not touch the error-axis behaviour it is layered onto.
    dispatch = _OutcomeDispatcher([("error", "unknown search provider 'The Verge'")])
    await run_tool_use_loop(
        llm=_StubbornLLM(),
        system="",
        user_input="go",
        tool_specs=[_VALIDATE],
        dispatch=dispatch,
        policy=_env(max_iterations=8),
    )
    assert len(dispatch.calls) == 2
