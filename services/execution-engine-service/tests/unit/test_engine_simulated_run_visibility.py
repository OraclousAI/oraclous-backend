"""#907 — a run executed by the scripted stand-in model must say so, on the wire.

Continues the harness-runtime-service half of this fix (``services/harness-runtime-service/tests/
unit/test_simulated_run_visibility.py``): once ``HarnessExecutionOut.simulated`` exists there, this
file pins how the engine LIFTS that fact from one harness call into the member's result payload
(``make_harness_dispatch``'s ``dispatch`` closure, ``services/team_run.py``), how a fail-closed
member failure says the LLM was simulated WITHOUT losing the real status/detail, and how it is
DERIVED upward onto ``TeamRunOut``/``TeamRunStatus``/``TeamRunStatusOut`` from ``results``.

RED until the [impl] lands: the dispatch payload has no "simulated" key, ``HarnessClientError``'s
message never mentions "simulated LLM", and none of ``TeamRunOut``/``TeamRunStatus``/
``TeamRunStatusOut`` has a ``simulated`` field — every assertion below fails with a KeyError,
AttributeError, or a plain failed assertion, never a skip.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.schema.engine_schemas import (
    TeamRunCost,
    TeamRunOut,
    TeamRunStatusOut,
)
from oraclous_execution_engine_service.services.harness_client import HarnessClientError
from oraclous_execution_engine_service.services.team_run import make_harness_dispatch
from oraclous_execution_engine_service.services.team_run_service import TeamRunStatus
from oraclous_ohm.manifest import OHMMember

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()


def _member(role: str = "a") -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", tools=[])


class _StubHarness:
    """Returns one scripted ``execute()`` result — never a real harness call."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        return self._result


# ── the dispatch closure lifts "simulated" into the member's result payload ──────────────────────


async def test_dispatch_payload_carries_simulated_true_from_the_harness_response() -> None:
    harness = _StubHarness(
        {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "done", "simulated": True}
    )
    dispatch = make_harness_dispatch(harness, {})
    payload = await dispatch(_member(), [], None)
    assert payload["simulated"] is True


async def test_dispatch_payload_defaults_simulated_false_when_the_key_is_absent() -> None:
    # Back-compat: a harness response from before this change carries no "simulated" key at all.
    harness = _StubHarness({"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "done"})
    dispatch = make_harness_dispatch(harness, {})
    payload = await dispatch(_member(), [], None)
    assert payload["simulated"] is False


async def test_failed_member_error_names_the_simulated_llm_without_losing_the_real_detail() -> None:
    # The fail-closed branch (make_harness_dispatch raises HarnessClientError on a non-SUCCEEDED,
    # non-PARTIAL status) must not SWALLOW the real status/detail behind the new note — both have
    # to be readable in the one message an operator sees.
    harness = _StubHarness(
        {
            "id": str(uuid.uuid4()),
            "status": "FAILED",
            "output": None,
            "error_message": "draft is required",
            "simulated": True,
        }
    )
    dispatch = make_harness_dispatch(harness, {})
    with pytest.raises(HarnessClientError) as exc:
        await dispatch(_member(), [], None)
    message = str(exc.value)
    assert "simulated LLM" in message
    assert "FAILED" in message
    assert "draft is required" in message


async def test_failed_member_error_omits_the_simulated_note_when_the_llm_was_real() -> None:
    # Non-regression pin, not a RED assertion: today's message already omits a note nobody writes
    # yet, so this passes on day one. It stays green after the [impl] lands (the note is added only
    # when simulated is True), which is exactly the guarantee this test is for.
    harness = _StubHarness(
        {
            "id": str(uuid.uuid4()),
            "status": "FAILED",
            "output": None,
            "error_message": "draft is required",
            "simulated": False,
        }
    )
    dispatch = make_harness_dispatch(harness, {})
    with pytest.raises(HarnessClientError) as exc:
        await dispatch(_member(), [], None)
    assert "simulated LLM" not in str(exc.value)


# ── TeamRunOut.simulated: derived from the run's per-member results ──────────────────────────────


def _team_run_out(results: dict[str, Any]) -> TeamRunOut:
    return TeamRunOut(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        state="SUCCEEDED",
        results=results,
        paused_at=[],
        error_message=None,
        created_at=None,
    )


def test_team_run_out_is_simulated_when_one_member_result_is_flagged() -> None:
    out = _team_run_out(
        {
            "a": {"output": "x", "status": "SUCCEEDED", "simulated": True},
            "b": {"output": "y", "status": "SUCCEEDED", "simulated": False},
        }
    )
    assert out.simulated is True


def test_team_run_out_is_not_simulated_when_every_result_is_none() -> None:
    # A blocked/skipped member's result is None (never a dict) — must not raise on .get().
    out = _team_run_out({"a": None, "b": None})
    assert out.simulated is False


def test_team_run_out_is_not_simulated_on_empty_results() -> None:
    out = _team_run_out({})
    assert out.simulated is False


# ── TeamRunStatus / TeamRunStatusOut carry the same fact ─────────────────────────────────────────


def test_team_run_status_dataclass_carries_simulated() -> None:
    status = TeamRunStatus(
        team_run_id=uuid.uuid4(),
        organisation_id=_ORG,
        healthy=True,
        state="SUCCEEDED",
        progress=100,
        last_run_at=None,
        last_outcome="SUCCEEDED",
        cost_tokens=100,
        simulated=True,
    )
    assert status.simulated is True


def test_team_run_status_out_schema_carries_simulated() -> None:
    out = TeamRunStatusOut(
        team_run_id=uuid.uuid4(),
        organisation_id=_ORG,
        healthy=True,
        state="SUCCEEDED",
        progress=100,
        last_run_at=None,
        last_outcome="SUCCEEDED",
        cost=TeamRunCost(tokens=100, usd=None),
        simulated=True,
    )
    assert out.simulated is True


def test_team_run_status_out_schema_defaults_simulated_false() -> None:
    out = TeamRunStatusOut(
        team_run_id=uuid.uuid4(),
        organisation_id=_ORG,
        healthy=True,
        state="SUCCEEDED",
        progress=100,
        last_run_at=None,
        last_outcome="SUCCEEDED",
        cost=TeamRunCost(tokens=0, usd=None),
    )
    assert out.simulated is False
