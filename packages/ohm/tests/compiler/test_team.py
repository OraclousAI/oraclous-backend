"""#594 — the compiler is a 4-member ACYCLIC team (no loop-SCC); the reviewer holds the validator.

CTO decision A: the repair loop is the reviewer's IN-HARNESS tool-use loop, so the team itself is a
plain linear chain with NO team-level loop and NO engine done-check.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.compiler.team import build_compiler_team
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def test_the_compiler_team_assembles_linear_and_acyclic() -> None:
    manifest, _subs = build_compiler_team(_ORG)
    loaded = load_ohm(manifest.model_dump(mode="json"))  # THE REAL loader
    assert loaded.is_team()
    assert loaded.execution_stages() == [
        ["planner"],
        ["capability-surveyor"],
        ["manifest-drafter"],
        ["reviewer"],
    ]
    # CTO decision A: NO team-level loop — the repair is the reviewer's own in-harness loop
    assert not (loaded.orchestration and loaded.orchestration.loops)


def test_the_reviewer_holds_the_validate_tool_others_are_reasoning_only() -> None:
    manifest, subs = build_compiler_team(_ORG)
    by = {m.role: m for m in manifest.members}
    assert by["reviewer"].tools == ["manifest-validate"]  # the in-harness repair calls validate
    assert all(by[r].tools == [] for r in ("planner", "capability-surveyor", "manifest-drafter"))
    assert set(subs) == {"planner", "capability-surveyor", "manifest-drafter", "reviewer"}


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
    from oraclous_ohm.compiler.team import _REPAIR_ATTEMPTS, _REVIEWER_VALIDATE_CALLS

    assert _REPAIR_ATTEMPTS == 2  # default 2 (CTO: default 2 / max 3)
    assert _REVIEWER_VALIDATE_CALLS == _REPAIR_ATTEMPTS + 1 == 3
    manifest, _ = build_compiler_team(_ORG)
    by = {m.role: m for m in manifest.members}
    assert by["reviewer"].max_tool_calls == _REVIEWER_VALIDATE_CALLS  # the harness halts here
    # only the reviewer is tool-call-bounded; the others are reasoning-only (no tools, no loop)
    assert all(
        by[r].max_tool_calls is None for r in ("planner", "capability-surveyor", "manifest-drafter")
    )


def test_the_objective_and_catalog_are_seeded_into_the_subgoals() -> None:
    # slice-1: the prose objective → the planner's subgoal; the catalog → the surveyor's. No engine
    # wiring — both render as the member's harness Objective: line (team_run._render_input).
    manifest, _ = build_compiler_team(
        _ORG, objective="Summarise the week's AI news into a digest.", catalog=["web-search"]
    )
    by = {m.role: m for m in manifest.members}
    assert by["planner"].subgoal == "Summarise the week's AI news into a digest."
    assert by["capability-surveyor"].subgoal is not None
    assert "web-search" in by["capability-surveyor"].subgoal  # the seeded catalog is in the subgoal


def test_the_drafter_is_seeded_to_emit_the_governance_policy() -> None:
    # #596: the drafter's subgoal seeds the governed-by-default policy template so the compiled team
    # carries governance (a known policy_set_ref) + the 3-layer budget.
    from oraclous_ohm.seeds import DEFAULT_POLICY_SET_REF

    manifest, _ = build_compiler_team(_ORG)
    drafter = {m.role: m for m in manifest.members}["manifest-drafter"]
    assert drafter.subgoal and DEFAULT_POLICY_SET_REF in drafter.subgoal
    assert "max_tokens_per_member" in drafter.subgoal  # the 3-layer budget is seeded
