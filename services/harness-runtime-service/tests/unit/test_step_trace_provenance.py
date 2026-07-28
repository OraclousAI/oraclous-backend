"""#641 — the tool_call_id rides the whole trace pipeline: LoopStep → JSONB → StepOut.

The loop-level stamping is proven in ``test_tool_use_loop.py``; this file proves the id then
SURVIVES persistence (``_serialize_steps`` → ``harness_executions.steps`` JSONB) and the read
DTO (``StepOut``), so the engine bridge can resolve a member's ``driving_signals`` claims against
the durable trace. RED until the [impl] adds ``LoopStep.tool_call_id`` + threads it through both.
"""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.loop.tool_use import LoopStep
from oraclous_harness_runtime_service.models.enums import StepKind
from oraclous_harness_runtime_service.schema.harness_schemas import StepOut
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
