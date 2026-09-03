"""#907 — a run executed by the scripted stand-in model must say so, on the wire.

#907 traced two symptoms (no team can be compiled or refined) to one cause: the stack ran with
``HARNESS_LLM_MODE=fake``, so ``FakeLLMClient`` (``domain/llm/fake.py``) executed every member and
no real model ran. Nothing on the gateway-facing API told the caller that. This file pins the FIRST
hop of the fix: the LLM client's own ``protocol_shape`` rides out of the tool-use loop
(``LoopResult.protocol_shape``), through step persistence (``_serialize_steps``), into the read
DTO (``HarnessExecutionOut.simulated``, a computed field mirroring the existing ``driving_signals``
field).

RED until the [impl] lands:
  - ``LoopResult`` has no ``protocol_shape`` attribute yet.
  - ``_serialize_steps`` does not accept a ``protocol_shape`` keyword.
  - ``StepOut`` has no ``protocol_shape`` field and ``HarnessExecutionOut`` has no ``simulated``
    computed field, so every assertion below fails with an AttributeError/TypeError, never a skip.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolSpec,
)
from oraclous_harness_runtime_service.domain.llm.fake import FakeLLMClient
from oraclous_harness_runtime_service.domain.loop.tool_use import LoopStep, run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind
from oraclous_harness_runtime_service.schema.harness_schemas import HarnessExecutionOut, StepOut
from oraclous_harness_runtime_service.services.harness_execution_service import _serialize_steps

pytestmark = pytest.mark.unit

_SPEC = ToolSpec(
    name="pg__list_tables",
    description="list tables",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="pg",
    operation="list_tables",
)


def _env() -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=6,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
    )


async def _ok_dispatch(spec: ToolSpec, args: dict) -> dict:
    return {"tables": ["a", "b"]}


class _RealShapedLLM:
    """A stub standing in for a REAL protocol client (never the fake): answers immediately, no
    tool call, so the loop's only job here is to read the client's own declared shape."""

    protocol_shape = "openai-compatible"

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        return LLMResponse(text="the real answer", tool_calls=[])


# ── LoopResult carries the client's protocol_shape (domain/loop/tool_use.py) ────────────────────


async def test_loop_result_carries_the_fake_clients_protocol_shape() -> None:
    result = await run_tool_use_loop(
        llm=FakeLLMClient(),
        system="s",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(),
    )
    assert result.status == HarnessStatus.SUCCEEDED
    assert result.protocol_shape == "fake"


async def test_loop_result_carries_a_real_clients_protocol_shape() -> None:
    result = await run_tool_use_loop(
        llm=_RealShapedLLM(),
        system="s",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(),
    )
    assert result.status == HarnessStatus.SUCCEEDED
    assert result.protocol_shape == "openai-compatible"


# ── persistence: _serialize_steps stamps protocol_shape on every emitted step ────────────────────


def test_serialize_steps_stamps_protocol_shape_on_every_step() -> None:
    steps = [
        LoopStep(index=0, kind=StepKind.LLM, name="primary", status="tool_calls", detail=None),
        LoopStep(
            index=1,
            kind=StepKind.TOOL,
            name="pg.list_tables",
            status="ok",
            detail=None,
            tool_call_id="c1",
        ),
    ]
    rows = _serialize_steps(steps, protocol_shape="fake")
    assert rows[0]["protocol_shape"] == "fake"
    assert rows[1]["protocol_shape"] == "fake"


def test_serialize_steps_stamps_a_real_protocol_shape_on_every_step() -> None:
    steps = [LoopStep(index=0, kind=StepKind.LLM, name="primary", status="answer", detail=None)]
    rows = _serialize_steps(steps, protocol_shape="openai-compatible")
    assert rows[0]["protocol_shape"] == "openai-compatible"


# ── the read DTO: HarnessExecutionOut.simulated, mirroring the #641 tool_call_id back-compat proof


def _execution_out(steps: list[StepOut]) -> HarnessExecutionOut:
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
        steps=steps,
        created_at=None,
    )


def test_step_out_exposes_the_protocol_shape() -> None:
    out = StepOut(
        index=0, kind=StepKind.LLM, name="primary", status="answer", protocol_shape="fake"
    )
    assert out.protocol_shape == "fake"
    assert out.model_dump()["protocol_shape"] == "fake"


def test_execution_out_is_simulated_when_any_step_ran_on_the_fake_protocol_shape() -> None:
    out = _execution_out(
        [
            StepOut(index=0, kind=StepKind.LLM, name="primary", status="tool_calls"),
            StepOut(
                index=1,
                kind=StepKind.TOOL,
                name="pg.list_tables",
                status="ok",
                tool_call_id="c1",
                protocol_shape="fake",
            ),
        ]
    )
    assert out.simulated is True


def test_execution_out_is_not_simulated_on_a_real_protocol_shape() -> None:
    out = _execution_out(
        [
            StepOut(
                index=0,
                kind=StepKind.LLM,
                name="primary",
                status="answer",
                protocol_shape="openai-compatible",
            )
        ]
    )
    assert out.simulated is False


def test_execution_out_is_not_simulated_on_a_pre_change_trace_with_no_protocol_shape_key() -> None:
    # Back-compat (mirrors #641's tool_call_id precedent): a trace persisted before this change
    # carries no "protocol_shape" key at all. The DTO must still parse — no validation error — and
    # report simulated=False rather than crash or silently claim the run was real.
    out = _execution_out([StepOut(index=0, kind=StepKind.LLM, name="primary", status="answer")])
    assert out.simulated is False
    assert out.model_dump()["simulated"] is False
