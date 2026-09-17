"""#1111 item 1 — the final-answer correction turn.

Decision 1 (posted on #1111, ruled as a standard default): a member that declares
``outputs_schema.required`` keys (``PolicyEnvelope.declared_output_keys`` — same source as the
#993/#997 shape guarantee, see ``test_declared_output_shape.py``) gets exactly ONE bounded
correction turn when its final answer either does not parse as JSON at all, or parses but omits one
of the declared keys. The correction message quotes the parser's own error position (so the model
can act on it, the #692/#693 lesson every correction in this file already applies) and names the
missing declared key(s). No manifest flag gates this (unlike #853's ``requires_valid_json``, which
governs a *different* declaration on a *different* call shape — a mid-loop ``graph-ingest`` tool
argument, not the member's own final answer) — it applies whenever ``declared_output_keys`` is set,
full stop.

This is deliberately a NEW, separate gate from the #993/#994/#1005 "shape guarantee" in
``test_declared_output_shape.py``, which is UNWRAP ONLY and spends no correction turn at all. That
ruling covers a value's SHAPE (nested vs. flat) once the key is present; this one covers the key's
PRESENCE (and the answer's basic parseability) before the shape guarantee ever runs.

Real shape (per the issue): a long object with ``posture``/``headline`` declared at the top level,
broken by an unescaped quote deep inside a later field — exactly the defect ``json.loads`` reports
as "Expecting ',' delimiter" mid-string, not at position 0, because the object opens and reads
cleanly for a while first.

RED until the [impl] adds the pre-SUCCEEDED correction turn to ``run_tool_use_loop`` in
``tool_use.py``, next to the existing (unwrap-only) ``policy.declared_output_keys`` block.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = pytest.mark.unit

# The issue's own defect, reproduced in miniature: a clean `posture`/`headline` prefix, then an
# unescaped quote inside `excerpt` breaks the object well past character 0 — `json.loads` fails at
# "line 1 column 96 (char 95)" here the same way the live document failed mid-way through, not at
# the start.
_BROKEN_FINAL_ANSWER = (
    '{"posture": "steady", "headline": "Fed holds rates", "excerpt": "analysts said the outlook '
    'is "choppy":"uncertain" into next quarter"}'
)
_FIXED_FINAL_ANSWER = json.dumps(
    {
        "posture": "steady",
        "headline": "Fed holds rates",
        "excerpt": 'analysts said the outlook is "choppy":"uncertain" into next quarter',
    }
)
# Parses cleanly, but omits the declared `headline` key entirely.
_MISSING_KEY_ANSWER = json.dumps({"posture": "steady", "excerpt": "clean text here"})
_FIXED_WITH_HEADLINE = json.dumps(
    {"posture": "steady", "headline": "Fed holds rates", "excerpt": "clean text here"}
)

_GATED = ToolSpec(
    name="approval__act",
    description="a capability behind a human-approval gate",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="needs-approval",
    operation="act",
)


def _envelope(
    *,
    declared_output_keys: tuple[str, ...] = ("posture", "headline"),
    requires_valid_json: bool = False,
    max_iterations: int = 8,
) -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=max_iterations,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
        declared_output_keys=declared_output_keys,
        requires_valid_json=requires_valid_json,
    )


class _Scripted:
    """One scripted final answer per model turn — no tool calls, ever (a tool-less member)."""

    protocol_shape = "fake"

    def __init__(self, *script: str) -> None:
        self._script = list(script)
        self.turns = 0
        # every role="user" message seen across the WHOLE run so far, in order — mirrors the
        # `test_json_repair_gate.py` convention: never index `[-1]` off this, scan the transcript.
        self.user_messages_seen: list[str] = []

    async def complete(self, *, messages: Any, system: str, tools: list[Any]) -> LLMResponse:
        self.user_messages_seen = [
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        ]
        entry = self._script[min(self.turns, len(self._script) - 1)]
        self.turns += 1
        return LLMResponse(text=entry, tool_calls=[])


async def _dispatch(_spec: Any, _args: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("a tool-less member never dispatches")


async def _run(llm: Any, *, policy: PolicyEnvelope) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="write your assigned brief",
        tool_specs=[],
        dispatch=_dispatch,
        policy=policy,
    )


# --- the issue's own scenario: broken escaping, one correction, the run survives --------------


async def test_a_final_answer_that_does_not_parse_gets_one_correction_and_then_succeeds() -> None:
    llm = _Scripted(_BROKEN_FINAL_ANSWER, _FIXED_FINAL_ANSWER, "done")
    result = await _run(llm, policy=_envelope())
    assert result.status is HarnessStatus.SUCCEEDED
    assert llm.turns == 2  # the correction earned exactly one more model turn, no more
    shipped = json.loads(result.output or "")
    assert shipped["posture"] == "steady"
    assert shipped["headline"] == "Fed holds rates"


async def test_the_correction_quotes_the_parser_error_position_and_names_the_missing_keys() -> None:
    llm = _Scripted(_BROKEN_FINAL_ANSWER, _FIXED_FINAL_ANSWER, "done")
    await _run(llm, policy=_envelope())
    # the model must read the parser's OWN message ("Expecting ',' delimiter: line 1 column 96
    # (char 95)"), not a generic "invalid JSON" — the #692/#693 lesson: an instruction the member
    # cannot act on is one it can only retry blindly.
    corrections = [m for m in llm.user_messages_seen if "line 1 column" in m]
    assert len(corrections) == 1  # exactly one correction, never re-sent on the retry's own reply
    assert "char" in corrections[0]
    # unparseable at all → neither declared key can be confirmed delivered, so both are named.
    assert "posture" in corrections[0]
    assert "headline" in corrections[0]


# --- a parseable answer missing one declared key gets the same one-shot treatment --------------


async def test_a_parseable_answer_missing_a_declared_key_gets_one_correction() -> None:
    llm = _Scripted(_MISSING_KEY_ANSWER, _FIXED_WITH_HEADLINE, "done")
    result = await _run(llm, policy=_envelope())
    assert result.status is HarnessStatus.SUCCEEDED
    assert llm.turns == 2
    shipped = json.loads(result.output or "")
    assert shipped["headline"] == "Fed holds rates"


async def test_the_correction_names_the_specific_missing_key() -> None:
    llm = _Scripted(_MISSING_KEY_ANSWER, _FIXED_WITH_HEADLINE, "done")
    await _run(llm, policy=_envelope())
    corrections = [m for m in llm.user_messages_seen if "headline" in m]
    assert len(corrections) == 1  # exactly one correction fired, and it names the missing key


# --- no manifest flag gates this — it is independent of #853's requires_valid_json -------------


async def test_the_correction_fires_without_the_requires_valid_json_flag() -> None:
    # Decision 1: unlike #853's `requires_valid_json` (a different declaration, on a different call
    # shape — a mid-loop `graph-ingest` tool argument), this correction needs no flag of its own. It
    # applies whenever `declared_output_keys` is set. `requires_valid_json` defaults False here and
    # is never turned on, so a pass proves the correction does not depend on it.
    llm = _Scripted(_BROKEN_FINAL_ANSWER, _FIXED_FINAL_ANSWER, "done")
    result = await _run(llm, policy=_envelope(requires_valid_json=False))
    assert result.status is HarnessStatus.SUCCEEDED
    assert llm.turns == 2


# --- the budget is bounded: a second bad answer gets no second correction ----------------------


async def test_a_second_unparseable_answer_gets_no_second_correction() -> None:
    # "Settles exactly as today" (the #853 precedent, `test_a_second_malformed_attempt_gets_no_
    # second_repair`): once the one-shot correction is spent, a still-bad answer is shipped exactly
    # as the member wrote it — no third model turn, no retry storm. Today's contract enforcement
    # (#697, `validate_payload` in the team orchestrator) is what ultimately fails a member over a
    # missing declared key; the harness loop itself has never refused to ship an answer on that
    # account, and this correction does not change that once its one shot is used.
    llm = _Scripted(_BROKEN_FINAL_ANSWER, _BROKEN_FINAL_ANSWER, "done")
    result = await _run(llm, policy=_envelope())
    assert llm.turns == 2  # the first attempt earned a correction; the second does not
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == _BROKEN_FINAL_ANSWER  # shipped unfixed, exactly as written


# --- a member with no declared keys is never checked --------------------------------------------


async def test_a_member_without_declared_output_keys_gets_no_correction_turn() -> None:
    llm = _Scripted(_BROKEN_FINAL_ANSWER, "done")
    result = await _run(llm, policy=_envelope(declared_output_keys=()))
    assert llm.turns == 1  # never re-prompted
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.output == _BROKEN_FINAL_ANSWER  # shipped exactly as it is today


# --- resumability: the one-shot state survives a LoopCheckpoint round-trip ----------------------


class _MixedScripted:
    """Turn script mixing bare final answers (a plain ``str``) and a gated tool call (the sentinel
    ``("gate",)``), mirroring ``test_json_repair_gate_resume_and_binding.py``'s tuple convention."""

    protocol_shape = "fake"

    def __init__(self, *script: Any) -> None:
        self._script = list(script)
        self.turns = 0

    async def complete(self, *, messages: Any, system: str, tools: list[Any]) -> LLMResponse:
        entry = self._script[min(self.turns, len(self._script) - 1)]
        self.turns += 1
        if entry == ("gate",):
            call = ToolCall(f"c{self.turns}", _GATED.name, {})
            return LLMResponse(text="requesting approval", tool_calls=[call])
        return LLMResponse(text=entry, tool_calls=[])


async def _gated_dispatch(_spec: Any, _args: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok"}


async def _run_gated(llm: Any, policy: PolicyEnvelope, resume: Any = None) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="write your assigned brief",
        tool_specs=[_GATED],
        dispatch=_gated_dispatch,
        policy=policy,
        resume_state=resume,
    )


def _gated_envelope() -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=8,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
        gated_bindings=frozenset({"needs-approval"}),
        declared_output_keys=("posture", "headline"),
    )


async def test_the_one_shot_correction_flag_survives_a_human_approval_pause() -> None:
    # Turn 1: a broken final answer earns the one correction. Turn 2: the member calls a gated
    # capability and the loop pauses for a human, carrying the (already-spent) correction state in
    # its `LoopCheckpoint` — the same #853-review lesson `test_json_repair_gate_resume_and_binding
    # .py` pins for the tool-content repair: state that does not ride the checkpoint either renews
    # itself every pause (a retry loop this feature exists not to be) or is lost outright.
    policy = _gated_envelope()
    llm = _MixedScripted(_BROKEN_FINAL_ANSWER, ("gate",), _BROKEN_FINAL_ANSWER)
    paused = await _run_gated(llm, policy)
    assert paused.status is HarnessStatus.ESCALATED
    assert paused.error_type == "hitl_required"
    assert paused.checkpoint is not None

    resumed = await _run_gated(llm, policy, resume=paused.checkpoint)
    # a second broken answer, AFTER the pause, must not earn a second correction: the one-shot
    # spent before the pause has to ride the checkpoint, not reset by it.
    assert resumed.status is HarnessStatus.SUCCEEDED
    assert resumed.output == _BROKEN_FINAL_ANSWER
    assert llm.turns == 3  # broken answer, gated call, second broken answer — no extra turn
