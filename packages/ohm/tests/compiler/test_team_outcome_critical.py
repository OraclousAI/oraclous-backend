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


# Second occurrence in this issue (the original #834 tests PR hit the same trap in its commit 5,
# fixing it the same way there): ANY stand-in dispatch for the reviewer role must carry this
# receipt, because the compiler manifest's reviewer declares the ``manifest-validate`` tool
# (`compiler/team.py`). A tool-declaring member is graded by #642's ``_grade_grounding``
# (`orchestrate.py`) BEFORE #834/#835's own rule ever runs: with no ``tool``-kind step whose
# ``status`` is ``"ok"`` and no ``driving_signal`` whose ``source_tool_call_id`` resolves to it,
# ``validate_grounding`` (`envelope.py`) fails the member on an unrelated "no receipt" error — and
# whatever terminal status the stand-in meant to produce (partial, succeeded, whatever) never
# survives to be asserted on. Reuse this constant in every new reviewer stand-in here rather than
# writing the steps/driving_signals shape out by hand again; unpack it into the payload (e.g.
# ``{"members": [...], **_REVIEWER_TOOL_RECEIPT}``) so ``_grade_grounding``'s ``pop`` on the
# dispatched dict never mutates this shared constant.
_REVIEWER_TOOL_RECEIPT: dict[str, Any] = {
    "steps": [
        {
            "index": 1,
            "kind": "tool",
            "name": "manifest-validate",
            "status": "ok",
            "tool_call_id": "tc-1",
        }
    ],
    "driving_signals": [{"signal": "validated", "value": True, "source_tool_call_id": "tc-1"}],
}


def _dispatch_reviewer_degrades(members_value: Any):
    """A dispatch stand-in: planner + manifest-drafter succeed normally; the reviewer degrades
    (PARTIAL) with its declared ``members`` key set to ``members_value`` — either missing/empty
    (case a: a broken draft the reviewer could not repair) or a genuinely delivered list
    (case b: a clean team the reviewer benignly re-validated past its cap, #596's own
    ``_REVIEWER_OVERCHECK_SLACK`` scenario).

    Carries ``_REVIEWER_TOOL_RECEIPT`` (see its comment) so these tests isolate the #834/#835 rule,
    not an unrelated #642 grounding failure."""

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role != "reviewer":
            return {"output": f"{member.role}-done"}
        payload: dict[str, Any] = {
            "status": "PARTIAL",
            "output": "gave up after re-validating",
            **_REVIEWER_TOOL_RECEIPT,
        }
        if members_value is not _ABSENT:
            payload["members"] = members_value
        return payload

    return dispatch


_ABSENT = object()


async def test_case_a_a_broken_draft_the_reviewer_could_not_repair_fails_the_run() -> None:
    # Correction (three independent reviewers on PR #1017, reproduced against unmodified code):
    # this dispatch has the declared "members" key ABSENT, not present-and-empty. A missing
    # declared required key trips the PRE-EXISTING #697 `validate_payload` check
    # (`envelope.py:74-86`), which runs unconditionally BEFORE the "did its best" (partial) branch
    # (`orchestrate.py:574-587`) and records the member "failed" — not #834/#835's new rule, which
    # only starts to matter once the key is PRESENT but empty (DESIGN §A.2). The implementation's
    # own docstring on `critical_member_lost_deliverable` says the same thing.
    manifest, _subs = build_compiler_team(_ORG)
    res = await run_team(manifest, _dispatch_reviewer_degrades(_ABSENT), cost_so_far=lambda: 0)
    assert res.member_status["reviewer"] == "failed"
    assert res.status == "failed"  # no team exists — the run must not report SUCCEEDED


async def test_case_a_variant_an_empty_members_list_also_fails_the_run() -> None:
    # This is the case the #834/#835 rule itself adds (present-but-empty, not missing) — the
    # declared "members" key IS present here, so #697's presence check passes and the run's
    # failure comes only from the new emptiness rule. The member therefore stays "partial", unlike
    # the case-a test above.
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


# ══ Part 2 (#834 follow-up, criterion 5) — the real compiler manifest, driven through "succeeded" ══  # noqa: E501
#
# The exact original #749 shape: the reviewer answers ``{"members": []}`` with no degrade at all —
# it settles SUCCEEDED, not PARTIAL. As shipped (PR #1017), the rule only checks a member recorded
# "partial", so this run still reports SUCCEEDED with zero rows in ``engine_team_drafts`` today.
# The orchestrator ruled this a blocker on #1017's security review. RED until the implementer
# widens the rule to check emptiness regardless of which terminal status it arrived on.


async def test_criterion5_the_reviewer_that_succeeds_with_an_empty_members_list_also_fails_the_run() -> (  # noqa: E501
    None
):
    manifest, _subs = build_compiler_team(_ORG)

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role != "reviewer":
            return {"output": f"{member.role}-done"}
        # No "status" key at all -> settles "succeeded", not "partial". Carries
        # _REVIEWER_TOOL_RECEIPT (see its comment above _dispatch_reviewer_degrades) so #642's
        # grounding grade — the reviewer declares the manifest-validate tool — doesn't fail this
        # member before the #834/#835 rule under test ever runs.
        return {"members": [], **_REVIEWER_TOOL_RECEIPT}

    res = await run_team(manifest, dispatch, cost_so_far=lambda: 0)
    assert res.member_status["reviewer"] == "succeeded"  # never relabelled
    assert res.status == "failed"  # no team exists — must not report SUCCEEDED
