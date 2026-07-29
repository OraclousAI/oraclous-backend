"""#641 — the tool_call_id rides the whole trace pipeline: LoopStep → JSONB → StepOut.

The loop-level stamping is proven in ``test_tool_use_loop.py``; this file proves the id then
SURVIVES persistence (``_serialize_steps`` → ``harness_executions.steps`` JSONB) and the read
DTO (``StepOut``), so the engine bridge can resolve a member's ``driving_signals`` claims against
the durable trace. RED until the [impl] adds ``LoopStep.tool_call_id`` + threads it through both.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_harness_runtime_service.domain.loop.tool_use import LoopStep
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind
from oraclous_harness_runtime_service.schema.harness_schemas import HarnessExecutionOut, StepOut
from oraclous_harness_runtime_service.services.harness_execution_service import _serialize_steps

pytestmark = pytest.mark.unit


def test_serialize_steps_carries_the_tool_call_id() -> None:
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
    rows = _serialize_steps(steps)
    assert rows[1]["tool_call_id"] == "c1"
    assert rows[0]["tool_call_id"] is None  # an LLM step has no tool call to point at


def test_step_out_exposes_the_tool_call_id() -> None:
    out = StepOut(
        index=0, kind=StepKind.TOOL, name="pg.list_tables", status="ok", tool_call_id="c1"
    )
    assert out.tool_call_id == "c1"
    assert out.model_dump()["tool_call_id"] == "c1"


def test_step_out_tool_call_id_defaults_to_none_for_old_traces() -> None:
    # Back-compat: rows persisted before #641 have no tool_call_id key — the DTO must still parse.
    out = StepOut(index=0, kind=StepKind.LLM, name="primary", status="answer")
    assert out.tool_call_id is None


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


def test_driving_signals_derived_from_ok_tool_steps_only() -> None:
    # #642 structural receipts: one signal per ok tool step with an id; llm steps, errored tool
    # steps, and id-less (pre-#641) tool steps derive nothing.
    out = _execution_out(
        [
            StepOut(index=0, kind=StepKind.LLM, name="primary", status="tool_calls"),
            StepOut(
                index=1,
                kind=StepKind.TOOL,
                name="manifest-validate",
                status="ok",
                tool_call_id="c1",
            ),
            StepOut(
                index=2, kind=StepKind.TOOL, name="graph-ingest", status="error", tool_call_id="c2"
            ),
            StepOut(index=3, kind=StepKind.TOOL, name="old-trace-tool", status="ok"),
        ]
    )
    assert out.driving_signals == [
        {"signal": "tool manifest-validate succeeded", "value": True, "source_tool_call_id": "c1"}
    ]


def test_driving_signals_empty_when_no_ok_tool_call() -> None:
    # Fail-closed shape: a run with no ok tool call reports an EMPTY list (never absent), so the
    # engine takes the structural [] over parsing the answer and the grounding grade fails.
    out = _execution_out([StepOut(index=0, kind=StepKind.LLM, name="primary", status="answer")])
    assert out.driving_signals == []
    assert out.model_dump()["driving_signals"] == []


def test_driving_signals_serialize_into_the_wire_payload() -> None:
    # The engine reads resp.json() — the computed field must be IN the serialized payload.
    out = _execution_out(
        [
            StepOut(
                index=0, kind=StepKind.TOOL, name="pg.list_tables", status="ok", tool_call_id="c9"
            )
        ]
    )
    dumped = out.model_dump(mode="json")
    assert dumped["driving_signals"] == [
        {"signal": "tool pg.list_tables succeeded", "value": True, "source_tool_call_id": "c9"}
    ]
