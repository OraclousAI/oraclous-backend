"""#835 — the compiler harness's reviewer member is the run's ``outcome_critical`` deliverable
(DESIGN §D; the first instance of #834's mechanism).

Instance 1 of the #749 class: the reviewer's output IS the deliverable (the assembled team) — when
it degrades with no repaired draft, ``engine_team_drafts`` gets zero rows, yet before #834/#835 the
run reported SUCCEEDED. Fix: mark the reviewer ``outcome_critical: true`` with
``outputs_schema={"required": ["members"]}`` (`compiler/team.py:167-180`). ``on_exhaustion:
"degrade"`` and ``max_tool_calls`` are UNCHANGED — this ticket does not touch retry behaviour.

RED until the [impl] lands: the reviewer carries neither field today.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.compiler.team import build_compiler_team
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMMember
from oraclous_ohm.orchestrate import run_team
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def test_the_reviewer_is_marked_outcome_critical() -> None:
    manifest, _subs = build_compiler_team(_ORG)
    by = {m.role: m for m in manifest.members}
    assert by["reviewer"].outcome_critical is True
    assert by["planner"].outcome_critical is False  # only the deliverable-producing member
    assert by["manifest-drafter"].outcome_critical is False


def test_the_reviewer_declares_members_as_its_required_output() -> None:
    manifest, _subs = build_compiler_team(_ORG)
    by = {m.role: m for m in manifest.members}
    required = by["reviewer"].outputs_schema.get("required")
    assert required == ["members"]


def test_the_flag_survives_the_real_loader() -> None:
    manifest, _subs = build_compiler_team(_ORG)
    loaded = load_ohm(manifest.model_dump(mode="json"))
    reviewer = loaded.member_by_role("reviewer")
    assert reviewer is not None
    assert reviewer.outcome_critical is True


def test_on_exhaustion_degrade_is_unchanged() -> None:
    # #835 does not touch retry behaviour — only #834's flag is new on this member.
    manifest, _subs = build_compiler_team(_ORG)
    by = {m.role: m for m in manifest.members}
    assert by["reviewer"].on_exhaustion == "degrade"


def test_max_tool_calls_is_unchanged() -> None:
    from oraclous_ohm.compiler.team import _REVIEWER_VALIDATE_CALLS

    manifest, _subs = build_compiler_team(_ORG)
    by = {m.role: m for m in manifest.members}
    assert by["reviewer"].max_tool_calls == _REVIEWER_VALIDATE_CALLS


# ── DESIGN §D end-to-end shapes — driven through the REAL compiler manifest + run_team ─────────


def _dispatch_reviewer_degrades(members_value: Any):
    """A dispatch stand-in: planner + manifest-drafter succeed normally; the reviewer degrades
    (PARTIAL) with its declared ``members`` key set to ``members_value`` — either missing/empty
    (case a: a broken draft the reviewer could not repair) or a genuinely delivered list
    (case b: a clean team the reviewer benignly re-validated past its cap, #596's own
    ``_REVIEWER_OVERCHECK_SLACK`` scenario).

    The reviewer declares the ``manifest-validate`` tool, so #642's grounding grade requires a
    receipt — a real ``tool``-kind step + a ``driving_signal`` backed by it — or the member fails
    on grounding BEFORE this issue's own rule ever runs. Both are included so these tests isolate
    the #834/#835 rule, not an unrelated #642 concern."""

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role != "reviewer":
            return {"output": f"{member.role}-done"}
        payload: dict[str, Any] = {
            "status": "PARTIAL",
            "output": "gave up after re-validating",
            "steps": [
                {
                    "index": 1,
                    "kind": "tool",
                    "name": "manifest-validate",
                    "status": "ok",
                    "tool_call_id": "tc-1",
                }
            ],
            "driving_signals": [
                {"signal": "validated", "value": True, "source_tool_call_id": "tc-1"}
            ],
        }
        if members_value is not _ABSENT:
            payload["members"] = members_value
        return payload

    return dispatch


_ABSENT = object()


async def test_case_a_a_broken_draft_the_reviewer_could_not_repair_fails_the_run() -> None:
    manifest, _subs = build_compiler_team(_ORG)
    res = await run_team(manifest, _dispatch_reviewer_degrades(_ABSENT), cost_so_far=lambda: 0)
    assert res.member_status["reviewer"] == "partial"
    assert res.status == "failed"  # no team exists — the run must not report SUCCEEDED


async def test_case_a_variant_an_empty_members_list_also_fails_the_run() -> None:
    manifest, _subs = build_compiler_team(_ORG)
    res = await run_team(manifest, _dispatch_reviewer_degrades([]), cost_so_far=lambda: 0)
    assert res.member_status["reviewer"] == "partial"
    assert res.status == "failed"


async def test_case_b_a_clean_team_over_checked_past_the_cap_still_completes() -> None:
    # NOT OPTIONAL — the RULING-1 regression guard for #835's own instance: a benign re-validation
    # of an already-clean draft (the deployed-stack scenario `_REVIEWER_OVERCHECK_SLACK` exists
    # for) must not fail the compile just because the reviewer degraded getting there.
    manifest, _subs = build_compiler_team(_ORG)
    res = await run_team(
        manifest,
        _dispatch_reviewer_degrades(["planner", "manifest-drafter", "reviewer"]),
        cost_so_far=lambda: 0,
    )
    assert res.member_status["reviewer"] == "partial"
    assert res.status == "completed"  # a real team was delivered — the run still completes
