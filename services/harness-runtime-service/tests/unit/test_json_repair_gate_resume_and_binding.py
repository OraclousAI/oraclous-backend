"""#853 review findings — two ways the one bounded repair turn failed to hold.

Both were found reviewing PR #856 and are regressions in waiting, not hypotheticals:

1. **The check was keyed on the capability's BINDING NAME.** An author who binds the same ingest
   capability under any other name got no check at all, silently — a malformed document went
   straight through and the run was lost exactly as it was before #853. Identity here is the
   OPERATION (``ingest``) plus the caller's own ``source_type``, never the author's chosen label.

2. **A human-approval pause dropped the repair state.** The granted extra tool call did not survive
   the pause, so a member that had already earned its repair came back to a budget gate and its
   CORRECTED document was refused — the fix rejecting the very document it had just asked for. The
   one-shot flag did not survive either, which is the same bug pointing the other way: a second
   repair after the pause, which is the retry loop this feature exists to not be.

RED until the [impl] keys the check on the operation and carries the repair state through
``LoopCheckpoint``.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]

_JSON_REPAIR_STATUS = "json_repair"

# The same capability, bound under a name the author chose. Nothing about the document changed.
_RENAMED_INGEST = ToolSpec(
    name="kb_write__ingest",
    description="ingest content into the knowledge graph",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="kb-write",
    operation="ingest",
)
_GATED = ToolSpec(
    name="approval__act",
    description="a capability behind a human-approval gate",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="needs-approval",
    operation="act",
)

_BROKEN = '{"sections": [{"a": 1}], {"b": 2}]}'
_FIXED = '{"sections": [{"a": 1}, {"b": 2}]}'


class _Scripted:
    """One scripted entry per model turn. A tuple is a turn's tool calls, each ``(spec, content)``
    for an ingest or ``(spec, None)`` for the gated capability; anything else is a final answer."""

    protocol_shape = "fake"

    def __init__(self, *script: Any) -> None:
        self._script = list(script)
        self.turns = 0

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        entry = self._script[min(self.turns, len(self._script) - 1)]
        self.turns += 1
        if not isinstance(entry, tuple):
            return LLMResponse(text=entry, tool_calls=[])
        calls = []
        for n, (spec, content) in enumerate(entry):
            args = (
                {"graph_id": "g1", "content": content, "source_type": "json"}
                if content is not None
                else {}
            )
            calls.append(ToolCall(f"c{self.turns}_{n}", spec.name, args))
        return LLMResponse(text="working", tool_calls=calls)


def _tracked_dispatch() -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def dispatch(_spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(args)
        return {"job_id": f"job{len(calls)}", "status": "queued"}

    return dispatch, calls


def _repairs(result: Any) -> list[Any]:
    return [s for s in result.steps if s.status == _JSON_REPAIR_STATUS]


async def _run(llm: Any, dispatch: Any, policy: Any, resume: Any = None) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="write the decision brief",
        tool_specs=[_RENAMED_INGEST, _GATED],
        dispatch=dispatch,
        policy=policy,
        resume_state=resume,
    )


# --- 1. the binding name is the author's label, not the platform's identity -------------------


async def test_a_renamed_ingest_binding_is_still_checked() -> None:
    policy = PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
        requires_valid_json=True,
    )
    llm = _Scripted(((_RENAMED_INGEST, _BROKEN),), ((_RENAMED_INGEST, _FIXED),), "done")
    dispatch, calls = _tracked_dispatch()
    result = await _run(llm, dispatch, policy)
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(_repairs(result)) == 1  # the check fired despite the unfamiliar binding name
    assert len(calls) == 1 and calls[0]["content"] == _FIXED


# --- 2. the repair state survives a human-approval pause -------------------------------------


def _gated_policy(max_tool_calls: int | None) -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=8,
        max_tool_calls=max_tool_calls,
        max_wall_time_seconds=None,
        max_tokens=None,
        gated_bindings=frozenset({"needs-approval"}),
        requires_valid_json=True,
    )


async def _pause_after_a_repair(policy: PolicyEnvelope, *rest: Any) -> tuple[Any, Any, list[Any]]:
    """Turn 1 asks for a malformed ingest AND a gated capability, so the repair is granted and the
    turn then pauses for a human — the exact interleaving that dropped the grant."""
    llm = _Scripted(((_RENAMED_INGEST, _BROKEN), (_GATED, None)), *rest)
    dispatch, calls = _tracked_dispatch()
    paused = await _run(llm, dispatch, policy)
    assert paused.status is HarnessStatus.ESCALATED
    assert paused.error_type == "hitl_required"
    assert paused.checkpoint is not None
    assert len(_repairs(paused)) == 1
    return llm, (paused, dispatch), calls


async def test_the_granted_repair_call_survives_a_human_approval_pause() -> None:
    # The member's own budget is one call, already earmarked for the gated capability. The repair
    # granted a second slot BEFORE the pause; if that grant does not come back, the corrected
    # document meets a budget gate and is thrown away — the fix rejecting its own repair.
    policy = _gated_policy(1)
    llm, (paused, dispatch), calls = await _pause_after_a_repair(
        policy, ((_RENAMED_INGEST, _FIXED),), "done"
    )
    resumed = await _run(llm, dispatch, policy, resume=paused.checkpoint)
    assert resumed.status is HarnessStatus.SUCCEEDED
    assert [c.get("content") for c in calls] == [None, _FIXED]


async def test_the_one_shot_flag_survives_a_human_approval_pause() -> None:
    # The same state, read the other way: a second malformed document AFTER the pause must not earn
    # a second correction. A repair that renews itself at every gate is the retry loop #853 refused.
    policy = _gated_policy(None)
    llm, (paused, dispatch), calls = await _pause_after_a_repair(
        policy, ((_RENAMED_INGEST, _BROKEN),), "done"
    )
    resumed = await _run(llm, dispatch, policy, resume=paused.checkpoint)
    assert len(_repairs(resumed)) == 0
    assert [c.get("content") for c in calls] == [None, _BROKEN]
