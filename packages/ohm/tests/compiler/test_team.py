"""#594 — the compiler is a 3-member ACYCLIC team (no loop-SCC); the reviewer holds the validator.

CTO decision A: the repair loop is the reviewer's IN-HARNESS tool-use loop, so the team itself is a
plain linear chain with NO team-level loop and NO engine done-check.

#709 deleted the capability-surveyor step: its only job was retyping the surveyed catalog as its
own output, and nothing read that output any more — the drafter gets the described catalog baked
into its own sub-goal (#713), and the reviewer's manifest-validate reads the org's live registry
directly (#705). The chain is now planner -> manifest-drafter -> reviewer.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.compiler.team import build_compiler_team
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def test_the_compiler_team_assembles_linear_and_acyclic() -> None:
    # #709: the capability-surveyor step is DELETED — nothing reads its output any more (the
    # drafter gets the described catalog straight from its own sub-goal, and the reviewer's
    # manifest-validate reads the org's live registry, never the surveyor's retyped list). The
    # chain is now planner -> manifest-drafter -> reviewer, three stages, not four.
    manifest, _subs = build_compiler_team(_ORG)
    loaded = load_ohm(manifest.model_dump(mode="json"))  # THE REAL loader
    assert loaded.is_team()
    assert loaded.execution_stages() == [
        ["planner"],
        ["manifest-drafter"],
        ["reviewer"],
    ]
    # CTO decision A: NO team-level loop — the repair is the reviewer's own in-harness loop
    assert not (loaded.orchestration and loaded.orchestration.loops)


def test_the_reviewer_holds_the_validate_tool_others_are_reasoning_only() -> None:
    manifest, subs = build_compiler_team(_ORG)
    by = {m.role: m for m in manifest.members}
    assert by["reviewer"].tools == ["manifest-validate"]  # the in-harness repair calls validate
    assert all(by[r].tools == [] for r in ("planner", "manifest-drafter"))
    assert set(subs) == {"planner", "manifest-drafter", "reviewer"}  # #709: no surveyor


def test_the_budget_is_the_three_layer_shape() -> None:
    manifest, _ = build_compiler_team(_ORG)
    b = manifest.budget
    assert b is not None
    assert b.max_tokens_total == 200_000 and b.max_sub_runs == 20  # the team pool (enforced axes)
    assert b.max_tokens_per_member == 60_000 and b.max_tokens_per_member <= b.max_tokens_total


def test_the_reviewer_repair_loop_is_hard_bounded_to_n_attempts() -> None:
    # CTO decision A / decision-3: the reviewer's in-harness validate→FIX→validate loop is bounded.
    # Each attempt is one manifest-validate call, so the bound is HARD-enforced by capping the
    # reviewer's max_tool_calls at _REPAIR_ATTEMPTS + 1 (the initial validate + at most N fixes) —
    # resolve_member_caps → the harness halts the loop at the cap regardless of the prompt. So a
    # persistently-blocked draft fail-closes after exactly N attempts; a draft needing ≤N repairs
    # converges within the cap.
    from oraclous_ohm.compiler.team import (
        _REPAIR_ATTEMPTS,
        _REVIEWER_OVERCHECK_SLACK,
        _REVIEWER_VALIDATE_CALLS,
    )

    assert _REPAIR_ATTEMPTS == 2  # default 2 fixes (CTO: default 2 / max 3)
    # #596: the HARD cap is the repair budget PLUS weak-model over-check slack — still bounded (no
    # runaway), but a clean compile no longer degrades because the model re-checked a passing draft.
    assert _REVIEWER_VALIDATE_CALLS == _REPAIR_ATTEMPTS + 1 + _REVIEWER_OVERCHECK_SLACK
    assert _REVIEWER_VALIDATE_CALLS > _REPAIR_ATTEMPTS + 1  # carries explicit over-check slack
    manifest, _ = build_compiler_team(_ORG)
    by = {m.role: m for m in manifest.members}
    assert by["reviewer"].max_tool_calls == _REVIEWER_VALIDATE_CALLS  # the harness halts here
    # only the reviewer is tool-call-bounded; the others are reasoning-only (no tools, no loop)
    assert all(by[r].max_tool_calls is None for r in ("planner", "manifest-drafter"))


def test_the_objective_is_seeded_into_the_planner_subgoal() -> None:
    # slice-1: the prose objective → the planner's subgoal. No engine wiring — it renders as the
    # member's harness Objective: line (team_run._render_input). #709: there is no surveyor left
    # to seed a catalog into; the described catalog reaching the drafter is
    # test_catalog_descriptions.py's job, not this test's.
    manifest, _ = build_compiler_team(_ORG, objective="Summarise the week's AI news into a digest.")
    by = {m.role: m for m in manifest.members}
    # the objective LEADS the planner's subgoal (followed by the seed topology shapes, #596)
    assert by["planner"].subgoal and by["planner"].subgoal.startswith(
        "Summarise the week's AI news into a digest."
    )
    assert "capability-surveyor" not in by  # #709: the step is gone


def test_the_drafter_is_seeded_to_emit_the_governance_policy() -> None:
    # #596: the drafter's subgoal seeds the governed-by-default policy template so the compiled team
    # carries governance (a known policy_set_ref) + the 3-layer budget.
    from oraclous_ohm.seeds import DEFAULT_POLICY_SET_REF

    manifest, _ = build_compiler_team(_ORG)
    drafter = {m.role: m for m in manifest.members}["manifest-drafter"]
    assert drafter.subgoal and DEFAULT_POLICY_SET_REF in drafter.subgoal
    assert "max_tokens_per_member" in drafter.subgoal  # the 3-layer budget is seeded


def test_the_planner_composes_from_the_seed_reference_topologies() -> None:
    # #596 DoD item 3 (CTO blocker fix): the planner's subgoal seeds the reference topology shape
    # names so it COMPOSES FROM them (never a frozen pipeline); the prose objective leads.
    manifest, _ = build_compiler_team(_ORG, objective="summarise the week's news")
    planner = {m.role: m for m in manifest.members}["planner"]
    assert planner.subgoal and "summarise the week's news" in planner.subgoal
    for shape in ("fan-out-fan-in", "standing-team", "gated-pipeline"):
        assert shape in planner.subgoal, f"the seed shape {shape!r} is seeded into the planner"


# ── the reviewer prompt must not fight the run's grounding directive ─────────


def test_reviewer_prompt_asks_for_the_grounding_receipt_alongside_the_team() -> None:
    """The reviewer declares ``manifest-validate``, so the engine appends GROUNDING_DIRECTIVE to
    its input and grades it on a ``driving_signals`` receipt (#642). This prompt used to answer
    that with "Reply IMMEDIATELY with ONLY that team JSON ... STOP", i.e. two instructions the
    model cannot both obey — so it satisfied one at random and compiles failed on a coin flip
    (run ``afc3b2c4``: 3 ok manifest-validate calls, a valid manifest, no receipt -> the member
    failed for unbacked claims; run ``8097a667``: receipt present -> the draft peel choked on it).
    Both instructions have to be satisfiable at once.
    """
    from oraclous_ohm.compiler.prompts import REVIEWER_PROMPT

    assert "driving_signals" in REVIEWER_PROMPT
    # the receipt is additive to the team JSON, never a replacement for it
    assert "BOTH required" in REVIEWER_PROMPT
    # and the exclusive phrasing that forbade it must not come back
    assert "ONLY that team JSON" not in REVIEWER_PROMPT


# ── #718: the drafter must justify every tool it hands out ──────────────────


def test_drafter_prompt_requires_a_tool_rationale_entry_per_tool() -> None:
    """The gate (validate.py's F-TOOL-UNJUSTIFIED) blocks a member that holds a tool with no stated
    reason, so the drafter has to be TOLD to fill `tool_rationale` before it ever gets there — the
    JSON example shows the field alongside `tools`, and a RULES bullet requires one entry per
    assigned tool."""
    from oraclous_ohm.compiler.prompts import DRAFTER_PROMPT

    assert '"tool_rationale"' in DRAFTER_PROMPT  # the JSON example shows the field
    assert DRAFTER_PROMPT.count("tool_rationale") >= 2  # the example AND a rule about it
    assert "this member" in DRAFTER_PROMPT.lower()  # ties the reason to THIS member, not the tool


def test_drafter_prompt_discourages_leaving_tools_empty_when_one_fits() -> None:
    """#718's companion team-level check (F-TEAM-NO-TOOLS) is confirm-severity, not blocking — so
    the only real lever against a team that hands out zero tools while the catalog offers one that
    fits is a soft nudge in the prompt itself."""
    from oraclous_ohm.compiler.prompts import DRAFTER_PROMPT

    assert "tools: []" in DRAFTER_PROMPT or "empty" in DRAFTER_PROMPT.lower()
