"""assemble_team -> OHM v1.1 Team Harness (#408; ADR-034 §6; ADR-043 §552 loop isolation).

Acyclic handoffs become depends_on edges (a pipeline). A GENUINE cycle is isolated as a Tarjan
strongly-connected component — an ``OHMOrchestration`` loop seam the conductor's bounded coordinator
runs (ADR-043) — while the acyclic remainder still runs on ``run_team``; a single back-edge among N
agents yields a SMALL loop, never a whole-team flip. The assembled team must load via the real
``load_ohm``. Schedules attach to members by role. Caller members are never mutated.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.import_.assemble import assemble_team
from oraclous_ohm.import_.handoff import HandoffSpec
from oraclous_ohm.import_.schedules import ScheduledJob
from oraclous_ohm.manifest import OHMMember
from oraclous_ohm.parse import load_ohm

# without this, the gate's `-m unit --strict-markers` job DESELECTS the whole file (it never runs)
pytestmark = pytest.mark.unit

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


# NB the pre-ADR-043 ``test_cyclic_handoffs_demote_to_routing`` (a cyclic graph demoted the WHOLE
# team to a routing-hint string with depends_on=[]) is SUPERSEDED by the ADR-043 loop-isolation
# tests below — ``test_full_team_cycle_is_a_single_loop_of_all_members`` covers the same 2-agent
# mutual handoff under the new contract (a loop seam, not a routing string).


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


# ── ADR-043 #552 step 1: Tarjan-SCC loop isolation (a small loop seam, not a whole-team flip) ──
#
# The pre-ADR-043 behaviour demoted the WHOLE handoff graph to a routing-hint string the moment ANY
# cycle existed (a single back-edge among N agents flipped the entire team to run-once). ADR-043
# isolates each GENUINE loop as a strongly-connected component: the acyclic remainder still becomes
# depends_on (runs on run_team), each SCC of >=2 members becomes an OHMOrchestration loop seam (the
# conductor's bounded coordinator runs it), and each member's ## Handoff next_task is preserved on
# the loop so the coordinator can re-dispatch with a concrete bounded objective.


def _loops(asm: object) -> list:
    return asm.manifest.orchestration.loops  # type: ignore[attr-defined]


def test_partial_cycle_isolates_a_small_scc_not_a_whole_team_flip() -> None:
    # a -> b -> c -> b (the loop {b,c}), c -> d -> e (the acyclic tail). A single back-edge (c->b)
    # must isolate ONLY {b,c} as a loop; a, d, e stay on the run_team skeleton with real depends_on
    # — the pre-ADR-043 code would have flipped ALL five members to depends_on=[] (run-once).
    members = [_m("a"), _m("b"), _m("c"), _m("d"), _m("e")]
    handoffs = {
        "a": HandoffSpec(next_agents=["b"], next_task="analyze"),
        "b": HandoffSpec(next_agents=["c"], next_task="critique"),
        "c": HandoffSpec(next_agents=["b", "d"], next_task="revise or finalize"),
        "d": HandoffSpec(next_agents=["e"], next_task="publish"),
    }
    asm = assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    loops = _loops(asm)
    assert len(loops) == 1
    assert set(loops[0].members) == {"b", "c"}  # ONLY the genuine cycle is a loop seam

    by = {m.role: m for m in asm.manifest.members}
    # the acyclic skeleton keeps its depends_on (NOT a whole-team flip):
    assert by["b"].depends_on == ["a"]  # the skeleton hands INTO the loop at b
    assert "c" in by["d"].depends_on  # the loop's output feeds the tail
    assert "d" in by["e"].depends_on
    # intra-loop edges (b<->c) are NOT depends_on — the coordinator routes them; the skeleton is DAG
    assert "c" not in by["b"].depends_on
    # the assembled team is acyclic over depends_on and loads through the real loader
    load_ohm(asm.manifest.model_dump(mode="json"))


def test_loop_routing_preserves_each_members_next_task() -> None:
    # the per-edge next_task is preserved on the loop seam so the coordinator re-dispatches the next
    # member with a concrete bounded objective (pre-ADR-043 discarded next_task entirely).
    members = [_m("analyst"), _m("critic")]
    handoffs = {
        "analyst": HandoffSpec(next_agents=["critic"], next_task="draft the section"),
        "critic": HandoffSpec(next_agents=["analyst"], next_task="list concrete revisions"),
    }
    asm = assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    loops = _loops(asm)
    assert len(loops) == 1
    routing = loops[0].routing
    assert routing["analyst"] == "draft the section"
    assert routing["critic"] == "list concrete revisions"


def test_full_team_cycle_is_a_single_loop_of_all_members() -> None:
    # a 2-agent mutual handoff (a<->b) is a genuinely fully-cyclic team: one loop seam of both
    # members (this supersedes the old whole-team routing-hint demotion).
    members = [_m("a"), _m("b")]
    handoffs = {"a": HandoffSpec(next_agents=["b"]), "b": HandoffSpec(next_agents=["a"])}
    asm = assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    loops = _loops(asm)
    assert len(loops) == 1
    assert set(loops[0].members) == {"a", "b"}
    assert asm.cyclic_routing is True  # a loop was isolated
    load_ohm(asm.manifest.model_dump(mode="json"))


def test_acyclic_handoffs_have_no_loop_seams() -> None:
    # a purely acyclic handoff graph (the EURail pipeline shape) yields NO loops — the conductor's
    # coordinator never engages; every edge is a depends_on on the run_team skeleton.
    members = [_m("a"), _m("b"), _m("c")]
    handoffs = {"a": HandoffSpec(next_agents=["b"]), "b": HandoffSpec(next_agents=["c"])}
    asm = assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    assert _loops(asm) == []
    assert asm.cyclic_routing is False
    by = {m.role: m for m in asm.manifest.members}
    assert by["b"].depends_on == ["a"] and by["c"].depends_on == ["b"]


def test_two_disjoint_loops_are_isolated_separately() -> None:
    # two independent 2-cycles ({a,b} and {c,d}) joined by an acyclic edge (b -> c) — each loop is a
    # SEPARATE seam, not merged, and the join is a skeleton depends_on.
    members = [_m("a"), _m("b"), _m("c"), _m("d")]
    handoffs = {
        "a": HandoffSpec(next_agents=["b"]),
        "b": HandoffSpec(next_agents=["a", "c"]),  # loop back to a, and forward to c
        "c": HandoffSpec(next_agents=["d"]),
        "d": HandoffSpec(next_agents=["c"]),  # loop back to c
    }
    asm = assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    loops = _loops(asm)
    assert len(loops) == 2
    member_sets = sorted(sorted(loop.members) for loop in loops)
    assert member_sets == [["a", "b"], ["c", "d"]]
    by = {m.role: m for m in asm.manifest.members}
    assert "b" in by["c"].depends_on  # the inter-loop join is a skeleton depends_on
    load_ohm(asm.manifest.model_dump(mode="json"))
