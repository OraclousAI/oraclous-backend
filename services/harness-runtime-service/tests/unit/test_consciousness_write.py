"""ADR-043 #554 (slice 3/3) — the five-family consciousness WRITE: a completed run's episodic memory
records the PATTERN (not a bare outcome), so a future run RECALLS a lesson. MVP = a CODED,
single-run classifier (NOT BYOM — a model grading its own run is a self-grade risk) over the
WITHIN-run families the deployed write path sees: ``repetitive_failures`` (a recurring in-run
error), ``velocity_anomaly`` (an over-long run), and — the compounding lesson — a SUCCESS
recorded as a reusable ``solution``. The genuinely cross-run families (hand-off friction,
recurring ambiguity, repetitive-across-stories) are a DEFERRED follow-up (the doc's sweep), per CTO.

``consciousness.permissions`` gates it: under ``never_auto_apply`` the memory is tagged NOT
auto-applicable (no harness changes its own behaviour without human review). Advisory-only — a
recalled lesson biases a turn but NEVER bypasses the coded done-check.

RED until ``classify_consciousness_pattern`` lands + ``schedule_run_outcome`` enriches the write.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()


# ── the coded classifier (pure, deterministic, never self-grades) ───────────────────────────────


def _classify(**kw: Any) -> str | None:
    from oraclous_harness_runtime_service.domain.consciousness import (
        classify_consciousness_pattern,
    )

    return classify_consciousness_pattern(**kw)


def test_success_is_recorded_as_a_reusable_solution() -> None:
    # the compounding lesson: a SUCCESS records its working approach for a future run to retrieve
    assert (
        _classify(status="SUCCEEDED", tool_names=["Write"], tool_errors=[], rounds=3) == "solution"
    )


def test_recurring_in_run_error_is_repetitive_failures() -> None:
    assert (
        _classify(status="FAILED", tool_names=["Read"], tool_errors=["boom", "boom"], rounds=3)
        == "repetitive_failures"
    )


def test_over_long_run_is_a_velocity_anomaly() -> None:
    assert (
        _classify(status="FAILED", tool_names=["Read"], tool_errors=[], rounds=25)
        == "velocity_anomaly"
    )


def test_unremarkable_failure_is_no_pattern() -> None:
    # a single failure with no recurring error + a normal length → no within-run pattern (None);
    # a cross-run sweep (deferred) is what would surface a pattern across many such runs
    assert _classify(status="FAILED", tool_names=["Read"], tool_errors=["once"], rounds=3) is None


def test_success_priority_over_length() -> None:
    # a SUCCESS is the applicable compounding lesson even if the run was long — record the solution
    assert (
        _classify(status="SUCCEEDED", tool_names=["Write"], tool_errors=[], rounds=30) == "solution"
    )


# ── the write enrichment — the recalled memory carries the pattern + the gate ───────────────────


def _capture_writer(seen: list[dict]):
    from oraclous_harness_runtime_service.services.memory_client import MemoryWriter

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content.decode()))
        return httpx.Response(201, json={"memory_id": "m1", "importance_score": 0.4})

    return MemoryWriter(
        base_url="http://knowledge-graph-service:8000",
        headers={"X-Internal-Key": "k", "X-Organisation-Id": str(_ORG)},
        timeout=2.0,
        transport=httpx.MockTransport(handler),
    )


async def test_run_outcome_write_records_the_pattern_and_the_never_auto_apply_gate() -> None:
    from oraclous_harness_runtime_service.services.memory_client import drain_pending_writes

    seen: list[dict] = []
    writer = _capture_writer(seen)
    writer.schedule_run_outcome(
        harness_id="h1",
        harness_name="Researcher",
        status="SUCCEEDED",
        user_input="gather evidence on X",
        output="found 3 sources via web-search",
        tool_names=["web-search", "Write"],
        execution_id=uuid.uuid4(),
        graph_id=str(uuid.uuid4()),
        team_id=str(uuid.uuid4()),
        tool_errors=[],
        rounds=4,
        can_auto_apply=False,  # the manifest's consciousness.permissions == never_auto_apply
    )
    await drain_pending_writes()

    assert len(seen) == 1
    body = seen[0]
    # the PATTERN is recorded (not a bare outcome) — a SUCCESS → the reusable "solution" lesson
    assert body["consciousness_pattern"] == "solution"
    assert "solution" in body["content"].lower()  # surfaces in the recalled content
    # gated: never auto-applied without human review (Flow-6 permission model)
    assert body["can_auto_apply"] is False
