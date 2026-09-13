"""#900 (ADR-053 decision 3) — the loop's three ``answer_from_tool`` termination boundaries.

ADR-053 rules that when a member's loop terminates on the tool named by its own
``answer_from_tool``, the PLATFORM writes the ``driving_signals`` receipt itself, from its own
record of the dispatch — never from the model. ``HarnessExecutionOut.driving_signals``
(``schema/harness_schemas.py``) already mints one receipt per successful (``status == "ok"``)
``StepKind.TOOL`` step of the run's OWN trace, generically, for every harness run, unconditionally
(pinned in ``test_step_trace_provenance.py``). So decision 3 needs NO new minting code anywhere —
it needs only the LOOP to, when the named tool's call completes ``ok``: (a) stop asking the model
for another turn, (b) use that call's own arguments as the member's answer, and (c) still record
the call as an ordinary, real ``StepKind.TOOL``/``status="ok"`` step (with its real
``tool_call_id``) in ``steps``, exactly as every other successful call already is. Do those three
things and ``driving_signals`` picks the receipt up for free.

**The boundary is exact and does not generalise (ADR-053 decision 3):** this exception fires only
when all three hold — the loop is terminating on the tool named by THIS member's own
``answer_from_tool``; the call is one the loop itself dispatched; and the platform's own record of
that dispatch shows it completed ``ok``. It never fires for a successful call to a DIFFERENT tool
(``test_a_successful_call_to_a_different_tool_does_not_end_the_loop``), and it never fires for a
FAILED dispatch of the named tool, even
(``test_a_failed_dispatch_of_the_named_tool_does_not_end_the_loop_or_mint_a_receipt``) — that
still feeds the error back for a retry turn, exactly as any other tool error does. With
``answer_from_tool`` unset (``None``, the default — every existing run today), nothing here
changes at all (``test_answer_from_tool_unset_leaves_ordinary_tool_calling_unaffected``).

**Identity used for the match: the capability BINDING, never the LLM-facing ``name`` or the bare
``operation``.** Three independent things in the surrounding code already agree on this: (1)
``policy.tool_ceiling`` — the existing capability-absence ceiling enforced right next to where this
boundary must sit — is built as ``frozenset(c.binding for c in manifest.capabilities)``
(``policy.py``) and checked in ``tool_use.py`` as ``spec.binding not in policy.tool_ceiling``, never
against ``spec.name``/``spec.operation``. (2) ``OHMMember.tools[]`` — the very list
``answer_from_tool`` names one entry of (``test_answer_from_tool_manifest.py``) — is a list of
capability bindings, the author's own aliases, not provider-facing function names (those are
sanitised/hashed per ``ToolSpec.name``, e.g. ``pg__list_tables``). (3) the already-merged envelope
test (``test_answer_from_tool_envelope.py::test_build_envelope_threads_answer_from_tool``) pins
``_envelope(member_answer_from_tool="web.search").answer_from_tool == "web.search"`` against a
manifest whose capability is bound as ``"web.search"`` — a binding, not an operation
(``search``) and not a provider function name. This file therefore compares
``spec.binding == policy.answer_from_tool`` at the same dispatch point ``tool_ceiling`` is already
checked (an out-of-ceiling tool can never be the answer tool either, though that combination is
not this file's subject).

RED until the ``[impl]`` lands: ``PolicyEnvelope`` carries no ``answer_from_tool`` field today (a
prior commit on this branch pinned it test-only in ``test_answer_from_tool_envelope.py`` — still
not built), so every ``_env(...)`` call below raises ``TypeError`` at construction, before the loop
is ever exercised. Not wrapped in ``pytest.raises`` — that would turn a RED failure into a
fabricated pass.
"""

from __future__ import annotations

import json
import uuid

import pytest
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)
from oraclous_harness_runtime_service.domain.loop.tool_use import LoopStep, run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind
from oraclous_harness_runtime_service.schema.harness_schemas import HarnessExecutionOut, StepOut

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]


# The tool a member DECLARES as its answer, and an unrelated second tool it might call instead.
_ANSWER_TOOL = ToolSpec(
    name="answer_tool__submit",
    description="submit the member's final structured answer",
    parameters={"type": "object", "properties": {"result": {"type": "string"}}, "required": []},
    binding="answer-tool",
    operation="submit",
)
_OTHER_TOOL = ToolSpec(
    name="other_tool__run",
    description="do something that is not answering",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="other-tool",
    operation="run",
)


def _env(
    *,
    max_iterations: int = 6,
    max_tool_calls: int | None = None,
    answer_from_tool: str | None = None,
) -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
        max_wall_time_seconds=None,
        max_tokens=None,
        gated_bindings=frozenset(),
        tool_ceiling=frozenset(),
        redact_patterns=(),
        answer_from_tool=answer_from_tool,
    )


class _ScriptedLLM:
    """Plays a fixed script of tool calls, one per turn, then answers with ``final`` free text.

    Counts every completion it was asked for (``self.calls``) — the decisive proof that a call
    which IS the member's answer (ADR-053 decision 3) never earns the model a further turn, and
    that an ordinary successful call to some OTHER tool earns it one exactly as today.
    """

    protocol_shape = "fake"

    def __init__(self, script: list[ToolCall], final: str = "the model's own final answer") -> None:
        self._script = list(script)
        self.final = final
        self.calls = 0

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        self.calls += 1
        if self._script:
            return LLMResponse(text="", tool_calls=[self._script.pop(0)])
        return LLMResponse(text=self.final)


async def _ok_dispatch(spec: ToolSpec, args: dict) -> dict:
    return {"received": args}


async def _boom_dispatch(spec: ToolSpec, args: dict) -> dict:
    raise RuntimeError("boom")


def _execution_out(steps: list[LoopStep]) -> HarnessExecutionOut:
    """Wrap a loop's real trace in the read DTO exactly as the service does, to prove
    ``driving_signals`` mints the ADR-053 receipt for free once the loop records the terminating
    call as an ordinary ok ``StepKind.TOOL`` step — mirrors
    ``test_step_trace_provenance.py::_execution_out``, the established home for this DTO."""
    return HarnessExecutionOut(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        harness_name="t",
        content_hash=None,
        status=HarnessStatus.SUCCEEDED,
        output="done",
        error_type=None,
        error_message=None,
        iterations=1,
        total_tokens=0,
        steps=[
            StepOut(
                index=s.index,
                kind=s.kind,
                name=s.name,
                status=s.status,
                detail=s.detail,
                tool_call_id=s.tool_call_id,
                started_at=s.started_at,
                ended_at=s.ended_at,
            )
            for s in steps
        ],
        created_at=None,
    )


# ── boundary 1+2: the named tool's OK call ends the loop, args become the answer ────────────────


async def test_a_successful_call_to_the_named_tool_ends_the_loop_and_becomes_the_answer() -> None:
    llm = _ScriptedLLM([ToolCall("c1", _ANSWER_TOOL.name, {"result": "final value"})])
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_ANSWER_TOOL, _OTHER_TOOL],
        dispatch=_ok_dispatch,
        policy=_env(answer_from_tool=_ANSWER_TOOL.binding),
    )
    # the ONE call the loop dispatched is the answer — the model is never asked for a second turn.
    assert llm.calls == 1
    assert result.status is HarnessStatus.SUCCEEDED
    assert json.loads(result.output or "") == {"result": "final value"}


async def test_the_terminating_call_is_a_real_ok_tool_step_that_mints_a_driving_signal() -> None:
    llm = _ScriptedLLM([ToolCall("c1", _ANSWER_TOOL.name, {"result": "final value"})])
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_ANSWER_TOOL, _OTHER_TOOL],
        dispatch=_ok_dispatch,
        policy=_env(answer_from_tool=_ANSWER_TOOL.binding),
    )
    tool_steps = [s for s in result.steps if s.kind is StepKind.TOOL]
    assert len(tool_steps) == 1
    assert tool_steps[0].status == "ok"
    assert tool_steps[0].tool_call_id == "c1"  # the platform's own record of the real call id

    # No new minting code needed anywhere (ADR-053): wrapping the SAME trace in the read DTO
    # already produces the receipt, because the terminating call is an ordinary ok tool step.
    execution = _execution_out(result.steps)
    assert execution.driving_signals == [
        {
            "signal": f"tool {_ANSWER_TOOL.binding}.{_ANSWER_TOOL.operation} succeeded",
            "value": True,
            "source_tool_call_id": "c1",
        }
    ]


# ── boundary 2: an ordinary call to a DIFFERENT tool never earns the exception ───────────────────


async def test_a_successful_call_to_a_different_tool_does_not_end_the_loop() -> None:
    llm = _ScriptedLLM(
        [ToolCall("c1", _OTHER_TOOL.name, {})], final="the real, later, model-written answer"
    )
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_ANSWER_TOOL, _OTHER_TOOL],
        dispatch=_ok_dispatch,
        policy=_env(answer_from_tool=_ANSWER_TOOL.binding),
    )
    # _OTHER_TOOL is not the declared answer tool — its ok call earns a second turn, as always.
    assert llm.calls == 2
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == "the real, later, model-written answer"


# ── boundary 3: a FAILED dispatch of the named tool never earns the exception ────────────────────


async def test_a_failed_dispatch_of_the_named_tool_does_not_end_the_loop_or_mint_a_receipt() -> (
    None
):
    llm = _ScriptedLLM(
        [
            ToolCall("c1", _ANSWER_TOOL.name, {"result": "attempt 1"}),
            ToolCall("c2", _ANSWER_TOOL.name, {"result": "attempt 2"}),
        ]
    )
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_ANSWER_TOOL, _OTHER_TOOL],
        dispatch=_boom_dispatch,
        policy=_env(max_iterations=2, answer_from_tool=_ANSWER_TOOL.binding),
    )
    # Never short-circuited into SUCCEEDED off an errored call — it fed the error back both times,
    # exactly as an ordinary tool error does, and ran out its normal (tiny) iteration budget.
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "iteration_cap"
    tool_steps = [s for s in result.steps if s.kind is StepKind.TOOL]
    assert len(tool_steps) == 2
    assert all(s.status == "error" for s in tool_steps)

    # No ok call of the named tool ever happened, so no receipt is minted — forged or otherwise.
    assert _execution_out(result.steps).driving_signals == []


# ── regression: answer_from_tool unset (None, today's default) is completely unaffected ─────────


async def test_answer_from_tool_unset_leaves_ordinary_tool_calling_unaffected() -> None:
    llm = _ScriptedLLM(
        [ToolCall("c1", _ANSWER_TOOL.name, {"result": "irrelevant to this run"})],
        final="ordinary free-text final answer",
    )
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_ANSWER_TOOL, _OTHER_TOOL],
        dispatch=_ok_dispatch,
        policy=_env(answer_from_tool=None),
    )
    # No declaration → calling _ANSWER_TOOL is just an ordinary tool call, exactly like today: it
    # never short-circuits the loop, and the model still writes the final answer itself.
    assert llm.calls == 2
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == "ordinary free-text final answer"
