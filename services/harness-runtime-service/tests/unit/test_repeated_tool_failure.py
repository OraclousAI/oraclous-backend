"""Unit (#946 T2): the loop stops dispatching a call that has already failed the same way twice.

A model handed an argument it can never satisfy — the search-vendor field of #946, filled with a
website name — does not learn from the failure. It re-sends the identical call until the tool-call
budget is gone, and the run ends as an anonymous "did not converge" that cost a full budget of real
calls to discover.

D2 (see ``tasks/plan.md``): the bound keys on the REPEATED CALL, not on an error taxonomy. The loop
dispatches to first-party connectors and imported servers alike, and no shared "this was a
validation error" signal crosses that boundary. An identical (tool, arguments, error) triple
repeating is observable without one, and it is the only signal that is honest for both sides.

Two dispatches of the identical failing call are allowed — the first could be transient, the second
proves it is not. The third is refused locally and the member is told once, in a sentence it can act
on. A call whose arguments changed is a different call and is dispatched normally.
"""

from __future__ import annotations

import json

import pytest
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)
from oraclous_harness_runtime_service.domain.loop.tool_use import (
    LoopCheckpoint,
    run_tool_use_loop,
)
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]


_SEARCH = ToolSpec(
    name="web_research__search",
    description="search the live web",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}, "provider": {"type": "string"}},
        "required": [],
    },
    binding="web-research",
    operation="search",
)


def _env(*, max_iterations: int = 8, max_tool_calls: int | None = None) -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
        max_wall_time_seconds=None,
        max_tokens=None,
        gated_bindings=frozenset(),
        tool_ceiling=frozenset(),
        redact_patterns=(),
    )


class _StubbornLLM:
    """Re-sends the identical failing call forever — the #946 model, exactly."""

    protocol_shape = "fake"

    def __init__(self, args: dict | None = None) -> None:
        self.args = {"query": "ai news", "provider": "The Verge"} if args is None else args
        self.turns = 0

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        self.turns += 1
        return LLMResponse(
            text="", tool_calls=[ToolCall(f"c{self.turns}", _SEARCH.name, dict(self.args))]
        )


class _AdaptingLLM:
    """Sends a different argument on every turn, then answers — a model that DOES adapt."""

    protocol_shape = "fake"

    def __init__(self) -> None:
        self.turns = 0

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        self.turns += 1
        if self.turns > 4:
            return LLMResponse(text="answer")
        return LLMResponse(
            text="",
            tool_calls=[
                ToolCall(f"c{self.turns}", _SEARCH.name, {"query": f"attempt {self.turns}"})
            ],
        )


class _Dispatcher:
    """Always raises the same error; records every call it was actually asked to make."""

    def __init__(self, message: str = "unknown search provider 'The Verge'") -> None:
        self.calls: list[dict] = []
        self.message = message

    async def __call__(self, spec: ToolSpec, args: dict) -> dict:
        self.calls.append(dict(args))
        raise RuntimeError(self.message)


def _tool_messages(result: object) -> list:
    return [s for s in result.steps if s.kind is StepKind.TOOL]  # type: ignore[attr-defined]


# --- the bound itself ---------------------------------------------------------------------------


async def test_the_same_call_failing_the_same_way_twice_is_not_dispatched_a_third_time() -> None:
    dispatch = _Dispatcher()
    await run_tool_use_loop(
        llm=_StubbornLLM(),
        system="",
        user_input="go",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=_env(max_iterations=8),
    )
    assert len(dispatch.calls) == 2


async def test_a_call_whose_arguments_changed_is_still_dispatched_normally() -> None:
    dispatch = _Dispatcher()
    await run_tool_use_loop(
        llm=_AdaptingLLM(),
        system="",
        user_input="go",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=_env(max_iterations=8),
    )
    # four distinct argument sets, four real dispatches — the bound never fired
    assert len(dispatch.calls) == 4
    assert len({json.dumps(a, sort_keys=True) for a in dispatch.calls}) == 4


async def test_argument_order_does_not_make_a_repeat_look_new() -> None:
    class _ReorderingLLM:
        protocol_shape = "fake"

        def __init__(self) -> None:
            self.turns = 0

        async def complete(
            self, *, messages: list[Message], system: str, tools: list[ToolSpec]
        ) -> LLMResponse:
            self.turns += 1
            args = (
                {"query": "ai news", "provider": "The Verge"}
                if self.turns % 2
                else {"provider": "The Verge", "query": "ai news"}
            )
            return LLMResponse(text="", tool_calls=[ToolCall(f"c{self.turns}", _SEARCH.name, args)])

    dispatch = _Dispatcher()
    await run_tool_use_loop(
        llm=_ReorderingLLM(),
        system="",
        user_input="go",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=_env(max_iterations=8),
    )
    assert len(dispatch.calls) == 2


# --- what the member is told ---------------------------------------------------------------------


async def test_the_member_is_told_in_a_sentence_it_can_act_on() -> None:
    dispatch = _Dispatcher()
    result = await run_tool_use_loop(
        llm=_StubbornLLM(),
        system="",
        user_input="go",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=_env(max_iterations=8),
    )
    refusals = [s for s in _tool_messages(result) if s.status == "repeated_failure"]
    assert refusals, "the refused call must be recorded as its own outcome, not as a plain error"
    detail = refusals[0].detail or ""
    # a sentence, not a status word or a class name
    assert " " in detail.strip()
    assert "RuntimeError" not in detail
    assert not detail.lstrip().startswith("{")


async def test_the_member_is_told_once_not_on_every_subsequent_turn() -> None:
    dispatch = _Dispatcher()
    result = await run_tool_use_loop(
        llm=_StubbornLLM(),
        system="",
        user_input="go",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=_env(max_iterations=8),
    )
    refusals = [s for s in _tool_messages(result) if s.status == "repeated_failure"]
    assert len(refusals) == 1


# --- settling, and surviving a resume -------------------------------------------------------------


async def test_a_member_whose_every_call_fails_still_settles() -> None:
    dispatch = _Dispatcher()
    result = await run_tool_use_loop(
        llm=_StubbornLLM(),
        system="",
        user_input="go",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=_env(max_iterations=8, max_tool_calls=20),
    )
    # it reaches a terminal without burning the whole tool-call budget on the same dead call
    assert result.status in {
        HarnessStatus.SUCCEEDED,
        HarnessStatus.PARTIAL,
        HarnessStatus.ESCALATED,
    }
    assert len(dispatch.calls) == 2


async def test_the_bound_survives_a_resumed_run() -> None:
    """A resume must not hand the member a fresh allowance for a call already proven dead.

    Mirrors #944's fetched-URL handling: the checkpoint carries the transcript, so the resumed
    segment re-derives the ledger from the restored ``tool``-role messages rather than starting
    empty. Without this, every HITL pause renews the retry loop the bound exists to stop.
    """
    args = {"query": "ai news", "provider": "The Verge"}
    error = json.dumps({"error": "RuntimeError", "detail": "unknown search provider 'The Verge'"})
    messages: list[Message] = [{"role": "user", "content": "go"}]
    for i in (1, 2):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"p{i}", "name": _SEARCH.name, "args": dict(args)}],
            }  # noqa: E501
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"p{i}",
                "name": _SEARCH.name,
                "content": f"{error}\n[receipt: source_tool_call_id=p{i} status=error]",
            }
        )
    checkpoint = LoopCheckpoint(
        messages=messages,
        pending_tool_calls=[],
        approved_tool_call_id="",
        iteration=2,
        tool_calls_made=2,
        tokens_used=0,
        redact_patterns=[],
    )
    dispatch = _Dispatcher()
    await run_tool_use_loop(
        llm=_StubbornLLM(args),
        system="",
        user_input="go",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=_env(max_iterations=8),
        resume_state=checkpoint,
    )
    assert dispatch.calls == []
