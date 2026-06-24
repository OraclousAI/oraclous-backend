"""AgentDefinition -> OHMMember + generated sub-harness mapping (#405 part 2; ADR-034 §2).

The headline invariant: the generated sub-harness MUST load through the REAL loader (``load_ohm``),
not just ``model_validate`` — that was the #402 lesson. Every silent default (model id, tool ref,
entrypoint, uuid) surfaces as an ImportFlag for the O8 dry-run rather than being resolved silently.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_.mapping import AgentMapping, map_agent_to_member
from oraclous_ohm.import_.parse import AgentDefinition
from oraclous_ohm.parse import load_ohm

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _agent(**kw: object) -> AgentDefinition:
    base: dict[str, object] = dict(
        name="diagram-generator",
        description="Creates diagrams.",
        model="sonnet",
        tools=["Read", "Grep", "Glob", "Write"],
        skills=[],
        body="You are diagram-generator.",
        source="diagram-generator.md",
    )
    base.update(kw)
    return AgentDefinition(**base)  # type: ignore[arg-type]


def _codes(m: AgentMapping) -> set[str]:
    return {f.code for f in m.flags}


def test_member_fields() -> None:
    m = map_agent_to_member(_agent(), owner_organization_id=_ORG)
    assert m.member.role == "diagram-generator"
    assert m.member.kind == "agent"
    assert m.member.manifest_ref == f"org:{_ORG}/diagram-generator@1"
    assert m.member.tools == ["Read", "Grep", "Glob", "Write"]  # raw ceiling, verbatim
    assert m.member.subgoal == "Creates diagrams."
    assert m.member.depends_on == []


def test_sub_harness_loads_through_the_real_loader() -> None:
    m = map_agent_to_member(_agent(), owner_organization_id=_ORG)
    assert m.sub_harness is not None
    loaded = load_ohm(m.sub_harness.model_dump(mode="json"))  # THE REAL ENTRY POINT (#402 lesson)
    assert loaded.is_team() is False
    assert loaded.primary_prompt() is not None
    assert loaded.primary_prompt().body == "You are diagram-generator."  # type: ignore[union-attr]
    assert loaded.primary_model().binding == "anthropic/claude-sonnet-4-6"  # type: ignore[union-attr]
    assert loaded.runtime.entrypoint == "primary"  # the agent actor (actors-path, not a tool)


def test_tools_become_capabilities_with_provisional_ref() -> None:
    # substrate="file" is the provisional-synthesis path: the file substrate keeps the synthesized
    # core/<slug>@1 refs (+ F-TOOLREF). Under the cloud-first graph default (#509) these file tools
    # remap onto seeded graph caps instead — that path is covered by test_import_graph_remap.py.
    m = map_agent_to_member(_agent(), owner_organization_id=_ORG, substrate="file")
    assert m.sub_harness is not None
    caps = {c.binding: c.ref for c in m.sub_harness.capabilities}
    assert caps == {
        "Read": "core/read@1",
        "Grep": "core/grep@1",
        "Glob": "core/glob@1",
        "Write": "core/write@1",
    }
    assert "F-TOOLREF" in _codes(m)


def test_model_tier_resolved_and_flagged() -> None:
    m = map_agent_to_member(_agent(model="opus"), owner_organization_id=_ORG)
    assert m.sub_harness is not None
    assert m.sub_harness.primary_model().binding == "anthropic/claude-opus-4-8"  # type: ignore[union-attr]
    assert "F-MODEL-RESOLVED" in _codes(m)


def test_model_absent_emits_no_model() -> None:
    m = map_agent_to_member(_agent(model=None), owner_organization_id=_ORG)
    assert m.sub_harness is not None
    assert m.sub_harness.models == []
    assert "F-MODEL-ABSENT" in _codes(m)


def test_model_unknown_passes_through_verbatim() -> None:
    m = map_agent_to_member(_agent(model="gpt-4o"), owner_organization_id=_ORG)
    assert m.sub_harness is not None
    assert m.sub_harness.primary_model().binding == "gpt-4o"  # type: ignore[union-attr]
    assert "F-MODEL-PASSTHROUGH" in _codes(m)


def test_empty_tools_is_reasoning_only() -> None:
    m = map_agent_to_member(_agent(tools=[]), owner_organization_id=_ORG)
    assert m.sub_harness is not None  # tool-less agents are valid (a reasoning-only actor)
    assert {f.code: f.severity for f in m.flags}.get("F-NOTOOLS") == "info"
    loaded = load_ohm(m.sub_harness.model_dump(mode="json"))  # loads with zero capabilities
    assert loaded.runtime.entrypoint == "primary"


def test_duplicate_tools_deduped_and_flagged() -> None:
    m = map_agent_to_member(_agent(tools=["Read", "Read", "Write"]), owner_organization_id=_ORG)
    assert m.member.tools == ["Read", "Write"]  # order-preserving de-dup
    assert "F-DUPTOOL" in _codes(m)


def test_subgoal_from_body_when_no_description() -> None:
    m = map_agent_to_member(
        _agent(description="", body="Mission: draw.\nMore."), owner_organization_id=_ORG
    )
    assert m.member.subgoal == "Mission: draw."
    assert "F-SUBGOAL-FROMBODY" in _codes(m)


def test_skills_deferred_flag() -> None:
    m = map_agent_to_member(_agent(skills=["graphify-aware"]), owner_organization_id=_ORG)
    assert "F-SKILLS-DEFERRED" in _codes(m)


def test_slug_flag_when_name_differs() -> None:
    m = map_agent_to_member(_agent(name="Diagram Generator"), owner_organization_id=_ORG)
    assert m.member.role == "diagram-generator"
    assert "F-SLUG" in _codes(m)


def test_human_gate_marker_flagged_but_still_agent() -> None:
    m = map_agent_to_member(
        _agent(body="The author uploads the final file."), owner_organization_id=_ORG
    )
    assert m.member.kind == "agent"  # detection only; kind:human is #408
    assert "F-HUMANGATE" in _codes(m)


def test_always_flags_idgen() -> None:
    assert "F-IDGEN" in _codes(map_agent_to_member(_agent(), owner_organization_id=_ORG))


def test_name_slugifying_to_empty_fails_closed() -> None:
    with pytest.raises(OHMImportError):
        map_agent_to_member(_agent(name="!!!"), owner_organization_id=_ORG)
