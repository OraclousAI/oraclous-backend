"""#828 item 2 — a step in the trace carries no time, so nothing can be ordered by the clock.

``StepOut`` has ``index`` and nothing else temporal. An index tells you the order steps happened in;
it does not tell you when, or how long any of them took. For a run measured in tens of minutes that
is the difference between "the tool call is the slow part" and a shrug.

The shape follows the precedent #641 set for ``tool_call_id``: two nullable fields with a ``None``
default, so every trace already in the database still validates and no migration is needed. The
times originate in ``LoopStep`` and ride out through ``_serialize_steps`` into the JSONB column;
asserting only on the DTO would let an implementation satisfy the schema while persisting nothing.

RED until ``LoopStep``, ``_serialize_steps`` and ``StepOut`` all carry the pair.
"""

from __future__ import annotations

import datetime as _dt
import uuid

import pytest
from oraclous_harness_runtime_service.domain.loop.tool_use import LoopStep
from oraclous_harness_runtime_service.models.enums import StepKind
from oraclous_harness_runtime_service.schema.harness_schemas import HarnessExecutionOut, StepOut

pytestmark = pytest.mark.unit

_T0 = _dt.datetime(2026, 8, 21, 9, 0, 0, tzinfo=_dt.UTC)
_T1 = _dt.datetime(2026, 8, 21, 9, 0, 12, tzinfo=_dt.UTC)


def test_a_step_carries_a_start_and_an_end() -> None:
    out = StepOut(
        index=0,
        kind=StepKind.TOOL,
        name="graph.search",
        status="ok",
        started_at=_T0,
        ended_at=_T1,
    )

    assert out.ended_at - out.started_at == _dt.timedelta(seconds=12)


def test_a_step_persisted_before_this_change_still_validates() -> None:
    # Back-compat, the #641 posture. Every trace already in the JSONB column lacks both keys; a
    # required field here would 500 every read of every historical execution.
    out = StepOut(index=0, kind=StepKind.LLM, name="primary", status="answer")

    assert out.started_at is None
    assert out.ended_at is None


def test_the_times_survive_serialization_into_the_trace_column() -> None:
    # The DTO is downstream of the JSONB write. If _serialize_steps drops the pair, every StepOut
    # built from a real row reads None while this file's first test stays green.
    from oraclous_harness_runtime_service.services.harness_execution_service import _serialize_steps

    rows = _serialize_steps(
        [
            LoopStep(
                index=0,
                kind=StepKind.TOOL,
                name="graph.search",
                status="ok",
                started_at=_T0,
                ended_at=_T1,
            )
        ]
    )

    assert rows[0]["started_at"] is not None
    assert rows[0]["ended_at"] is not None

    execution = HarnessExecutionOut(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        harness_name="h",
        content_hash=None,
        status="SUCCEEDED",
        output="done",
        error_type=None,
        error_message=None,
        iterations=1,
        total_tokens=10,
        steps=rows,  # type: ignore[arg-type]
        created_at=_T0,
    )

    step = execution.steps[0]
    assert step.started_at == _T0
    assert step.ended_at == _T1


def test_a_steps_end_is_never_before_its_start() -> None:
    with pytest.raises(ValueError, match="ended_at"):
        StepOut(
            index=0,
            kind=StepKind.TOOL,
            name="graph.search",
            status="ok",
            started_at=_T1,
            ended_at=_T0,
        )
