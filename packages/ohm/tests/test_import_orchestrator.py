"""Single-skill-orchestrator adapter (#407; ADR-034 §5).

An orchestrator skill with a ``modules/<wave>/*.md`` layout becomes an OHM v1.1 ``orchestration``
block + a ``members[]`` DAG: one member per module (each a distinct brief), wave order derived from
the global ``NN-`` numeric prefix, ``depends_on`` = all of the previous wave (fan-in barrier). The
members must resolve through the real ``topological_stages`` to the expected wave-stage spine.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from oraclous_ohm.dag import topological_stages
from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_.orchestrator import (
    OrchestratorPlan,
    adapt_orchestrator_skill,
    adapt_orchestrator_skill_by_name,
)
from oraclous_ohm.import_.skills import resolve_skill

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")

_SKILL = """---
name: myorch
description: A report. Fans out 3 research subagents in parallel, then analysis, then a gate.
---
## Phases
Sequential waves; within each wave, all subagents run in parallel.

## Hard rules
- Every claim needs a citation.
- Conflicts are resolved or carried open.

Run `/myorch --quick` to skip deep dives, or `/myorch --refresh-from <ledger>` to re-verify.
"""

_MODULES = {
    "research": ["01-alpha.md", "02-beta.md", "03-gamma.md"],
    "analysis": ["04-delta.md", "05-epsilon.md"],
    "gates": ["06-zeta.md"],
}


def _make_orch(root: Path, name: str = "myorch", *, with_modules: bool = True) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_SKILL)
    if not with_modules:
        return
    for wave, files in _MODULES.items():
        wd = d / "modules" / wave
        wd.mkdir(parents=True)
        for f in files:
            (wd / f).write_text(f"# {f[:-3]} brief\nResearch {f}.\n")


def _adapt(root: Path) -> OrchestratorPlan:
    return adapt_orchestrator_skill(
        resolve_skill("myorch", root), owner_organization_id=_ORG, skills_root=root
    )


def _codes(p: OrchestratorPlan) -> set[str]:
    return {f.code for f in p.flags}


def test_one_member_per_module(tmp_path: Path) -> None:
    _make_orch(tmp_path)
    plan = _adapt(tmp_path)
    assert {m.role for m in plan.members} == {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}
    assert all(m.kind == "agent" for m in plan.members)
    assert any(m.manifest_ref == f"org:{_ORG}/alpha@1" for m in plan.members)


def test_wave_order_and_depends_on(tmp_path: Path) -> None:
    _make_orch(tmp_path)
    plan = _adapt(tmp_path)
    by_role = {m.role: m for m in plan.members}
    assert by_role["alpha"].depends_on == []  # first wave
    assert set(by_role["delta"].depends_on) == {"alpha", "beta", "gamma"}  # analysis ← all research
    assert set(by_role["zeta"].depends_on) == {"delta", "epsilon"}  # gates ← all analysis


def test_topological_stages_is_the_wave_spine(tmp_path: Path) -> None:
    _make_orch(tmp_path)
    stages = topological_stages(_adapt(tmp_path).members)
    assert stages == [["alpha", "beta", "gamma"], ["delta", "epsilon"], ["zeta"]]


def test_subgoal_from_module_heading(tmp_path: Path) -> None:
    _make_orch(tmp_path)
    plan = _adapt(tmp_path)
    assert {m.role: m.subgoal for m in plan.members}["alpha"] == "01-alpha brief"


def test_orchestration_populated(tmp_path: Path) -> None:
    _make_orch(tmp_path)
    plan = _adapt(tmp_path)
    assert plan.orchestration.medium == ["blackboard"]
    assert plan.orchestration.style
    assert "citation" in plan.orchestration.success_criteria
    assert "F-MEDIUM-INFERRED" in _codes(plan)
    assert "F-TERMINATION-ABSENT" in _codes(plan)


def test_conditional_modes_surfaced_not_branched(tmp_path: Path) -> None:
    _make_orch(tmp_path)
    plan = _adapt(tmp_path)
    assert "--quick" in plan.conditional_modes
    assert "--refresh-from" in plan.conditional_modes
    assert any(c.startswith("F-MODE-") for c in _codes(plan))


def test_large_wave_flagged_fanout_candidate(tmp_path: Path) -> None:
    # research has 3 here; make a 4+ wave to trigger the candidate flag
    _make_orch(tmp_path, "myorch")
    big = tmp_path / "myorch" / "modules" / "research"
    (big / "00-extra.md").write_text("# extra\n")  # now 4 research modules
    assert "F-FANOUT-CANDIDATE" in _codes(_adapt(tmp_path))


def test_leaf_skill_rejected(tmp_path: Path) -> None:
    d = tmp_path / "leafy"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: leafy\ndescription: A plain helper.\n---\nJust do the thing."
    )
    with pytest.raises(OHMImportError):
        adapt_orchestrator_skill(
            resolve_skill("leafy", tmp_path), owner_organization_id=_ORG, skills_root=tmp_path
        )


def test_no_modules_layout_blocks(tmp_path: Path) -> None:
    _make_orch(tmp_path, with_modules=False)
    plan = _adapt(tmp_path)
    assert plan.members == []
    assert {f.code: f.severity for f in plan.flags}.get("F-ORCH-UNSTRUCTURED") == "blocking"


def test_by_name_wrapper(tmp_path: Path) -> None:
    _make_orch(tmp_path)
    plan = adapt_orchestrator_skill_by_name("myorch", tmp_path, owner_organization_id=_ORG)
    assert len(plan.members) == 6
