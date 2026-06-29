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
