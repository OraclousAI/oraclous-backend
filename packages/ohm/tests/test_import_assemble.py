"""assemble_team -> OHM v1.1 Team Harness (#408; ADR-034 §6).

Acyclic handoffs become depends_on edges (a pipeline); a cyclic handoff graph is a standing team,
so the edges demote to routing-hints (not forced into a DAG). The assembled team must load via
the real ``load_ohm``. Schedules attach to members by role. Caller members are never mutated.
"""

from __future__ import annotations

import uuid

from oraclous_ohm.import_.assemble import assemble_team
from oraclous_ohm.import_.handoff import HandoffSpec
from oraclous_ohm.import_.schedules import ScheduledJob
from oraclous_ohm.manifest import OHMMember
from oraclous_ohm.parse import load_ohm

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, depends_on: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=depends_on or []
    )


def _codes(asm: object) -> set[str]:
    return {f.code for f in asm.flags}  # type: ignore[attr-defined]


def test_acyclic_handoffs_become_depends_on() -> None:
    members = [_m("a"), _m("b"), _m("c")]
    handoffs = {"a": HandoffSpec(next_agents=["b"]), "b": HandoffSpec(next_agents=["c"])}
    asm = assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    assert asm.cyclic_routing is False
    by = {m.role: m for m in asm.manifest.members}
    assert by["b"].depends_on == ["a"]  # a hands to b -> b depends_on a
    assert by["c"].depends_on == ["b"]
    assert asm.manifest.is_team()
    assert "F-HANDOFF-EDGES" in _codes(asm)


def test_assembled_team_loads_through_the_real_loader() -> None:
    members = [_m("a"), _m("b")]
    handoffs = {"a": HandoffSpec(next_agents=["b"])}
    asm = assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    loaded = load_ohm(asm.manifest.model_dump(mode="json"))  # THE REAL ENTRY POINT
    assert loaded.is_team()
    assert loaded.execution_stages() == [["a"], ["b"]]


def test_cyclic_handoffs_demote_to_routing() -> None:
    members = [_m("a"), _m("b")]
    handoffs = {"a": HandoffSpec(next_agents=["b"]), "b": HandoffSpec(next_agents=["a"])}
    asm = assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    assert asm.cyclic_routing is True
    assert "F-CYCLIC-ROUTING" in _codes(asm)
    by = {m.role: m for m in asm.manifest.members}
    assert by["a"].depends_on == [] and by["b"].depends_on == []  # NOT forced into a DAG
    assert asm.manifest.orchestration is not None
    assert "Handoff routing" in asm.manifest.orchestration.style
    load_ohm(asm.manifest.model_dump(mode="json"))  # still loads


def test_schedules_attached_and_team_loads() -> None:
    members = [_m("analyst"), _m("osint-analyst")]
    sched = [ScheduledJob(id="mb", cron="0 7 * * *", agent="analyst")]
    asm = assemble_team("t", members, owner_organization_id=_ORG, schedules=sched)
    by = {m.role: m for m in asm.manifest.members}
    assert by["analyst"].schedule == "0 7 * * *"
    assert "F-SCHEDULE-ATTACHED" in _codes(asm)
    load_ohm(asm.manifest.model_dump(mode="json"))  # the schedule field doesn't break loading


def test_schedule_unknown_agent_flagged() -> None:
    members = [_m("analyst")]
    sched = [ScheduledJob(id="x", cron="0 0 * * *", agent="ghost")]
    asm = assemble_team("t", members, owner_organization_id=_ORG, schedules=sched)
    assert {f.code: f.severity for f in asm.flags}.get("F-SCHEDULE-NOMATCH") == "confirm"


def test_caller_members_not_mutated() -> None:
    members = [_m("a"), _m("b")]
    handoffs = {"a": HandoffSpec(next_agents=["b"])}
    assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    assert members[1].depends_on == []  # original untouched (deep-copied inside)
