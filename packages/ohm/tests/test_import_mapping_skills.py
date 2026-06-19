"""map_agent_to_member skill integration (#406; ADR-034 §3).

With a ``skills_root``, leaf skills are inlined into the sub-harness prompt (which still loads via
the real loader), orchestrator skills are flagged for #407, and a missing skill blocks — F-SKILLS-
DEFERRED is superseded. Without a ``skills_root`` the #405 deferral behaviour is unchanged.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from oraclous_ohm.import_.mapping import AgentMapping, map_agent_to_member
from oraclous_ohm.import_.parse import AgentDefinition
from oraclous_ohm.parse import load_ohm

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_LEAF = "---\nname: bible-keeper\ndescription: Curator.\n---\n# bible-keeper\nYou synthesize canon."
_ORCH = "---\nname: book-calibrate\ndescription: Coordinator.\n---\nFans out 5 agents in parallel."


def _agent(skills: list[str]) -> AgentDefinition:
    return AgentDefinition(
        name="curator",
        description="Curate.",
        model="opus",
        tools=["Read", "Write"],
        skills=skills,
        body="You curate the bible.",
        source="curator.md",
    )


def _skill(root: Path, name: str, text: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text)


def _codes(m: AgentMapping) -> set[str]:
    return {f.code for f in m.flags}


def test_leaf_skill_inlined_into_sub_harness(tmp_path: Path) -> None:
    _skill(tmp_path, "bible-keeper", _LEAF)
    m = map_agent_to_member(
        _agent(["bible-keeper"]), owner_organization_id=_ORG, skills_root=tmp_path
    )
    assert "F-SKILL-RESOLVED" in _codes(m)
    assert "F-SKILLS-DEFERRED" not in _codes(m)  # superseded once resolution happens
    assert m.sub_harness is not None
    loaded = load_ohm(m.sub_harness.model_dump(mode="json"))  # the inlined prompt still loads
    body = loaded.primary_prompt().body  # type: ignore[union-attr]
    assert body.startswith("You curate the bible.")
    assert "## Available Skills" in body
    assert "You synthesize canon" in body


def test_orchestrator_skill_flagged_not_inlined(tmp_path: Path) -> None:
    _skill(tmp_path, "book-calibrate", _ORCH)
    m = map_agent_to_member(
        _agent(["book-calibrate"]), owner_organization_id=_ORG, skills_root=tmp_path
    )
    assert "F-SKILL-ORCHESTRATOR" in _codes(m)
    assert m.sub_harness is not None
    loaded = load_ohm(m.sub_harness.model_dump(mode="json"))
    assert "## Available Skills" not in loaded.primary_prompt().body  # type: ignore[union-attr]


def test_missing_skill_blocks(tmp_path: Path) -> None:
    m = map_agent_to_member(_agent(["ghost"]), owner_organization_id=_ORG, skills_root=tmp_path)
    assert {f.code: f.severity for f in m.flags}.get("F-SKILL-MISSING") == "blocking"


def test_skills_root_none_still_defers() -> None:
    m = map_agent_to_member(_agent(["bible-keeper"]), owner_organization_id=_ORG)
    assert "F-SKILLS-DEFERRED" in _codes(m)
