"""#577 (sub-feature 1) — the acyclic path threads the producer's ``## Handoff`` Next-task into the
consumer's ``objective_slice``, mirroring the loop path.

Today the loop dispatch uses the per-edge routing (orchestrate.py:511 — ``loop.routing.get``), but
the ACYCLIC dispatch hardcodes ``objective_slice=member.subgoal`` (orchestrate.py:225, :392),
dropping the per-edge handoff task — so a consumer gets a static blurb ("Chapter XX") instead of the
producer's scoped objective ("Draft Chapter 04"). This wires the existing ``## Handoff`` Next-task
(already parsed by handoff.py) onto the producing member and into the acyclic objective_slice.

Artifact-independent: the ``## Handoff`` convention is general (bitcoin + the book charters), so it
is testable with a synthetic two-member team — no book-specific coordinator parsing.

RED until the [impl] adds ``OHMMember.handoff_objective``, sets it in ``assemble_team``, and uses it
in ``run_team``'s acyclic dispatch.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime
from oraclous_ohm.orchestrate import run_team

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(
    role: str,
    depends_on: list[str] | None = None,
    *,
    subgoal: str | None = None,
    handoff_objective: str | None = None,
) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=depends_on or [],
        subgoal=subgoal,
        handoff_objective=handoff_objective,
    )


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _edge(res: Any, frm: str, to: str) -> HandoffEnvelope:
    return next(e for e in res.envelopes if e.from_role == frm and e.to_role == to)


# ── the OHM field ──────────────────────────────────────────────────────────────────────────────


def test_member_accepts_handoff_objective() -> None:
    m = OHMMember(
        role="writer", kind="agent", manifest_ref="org:x/a@1", handoff_objective="review the draft"
    )
    assert m.handoff_objective == "review the draft"


def test_handoff_objective_defaults_to_none_back_compat() -> None:
    m = OHMMember(role="writer", kind="agent", manifest_ref="org:x/a@1")
    assert m.handoff_objective is None  # unchanged manifests parse as before


# ── assemble_team sets it from the ## Handoff Next-task ───────────────────────────────────────────


def test_assemble_sets_handoff_objective_from_next_task() -> None:
    from oraclous_ohm.import_.assemble import assemble_team
    from oraclous_ohm.import_.handoff import HandoffSpec

    members = [_m("writer"), _m("critic")]
    handoffs = {
        "writer": HandoffSpec(next_agents=["critic"], next_task="review the draft for accuracy")
    }
    asm = assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    by = {m.role: m for m in asm.manifest.members}
    assert by["critic"].depends_on == ["writer"]  # the edge still forms (unchanged)
    assert by["writer"].handoff_objective == "review the draft for accuracy"  # + the per-edge task


def test_assemble_leaves_handoff_objective_none_without_a_next_task() -> None:
    from oraclous_ohm.import_.assemble import assemble_team
    from oraclous_ohm.import_.handoff import HandoffSpec

    members = [_m("writer"), _m("critic")]
    handoffs = {"writer": HandoffSpec(next_agents=["critic"])}  # no next_task
    asm = assemble_team("t", members, owner_organization_id=_ORG, handoffs=handoffs)
    by = {m.role: m for m in asm.manifest.members}
    assert by["writer"].handoff_objective is None  # nothing to carry → unchanged


# ── run_team threads it into the acyclic objective_slice (mirrors the loop path) ──────────────────


async def test_acyclic_objective_slice_uses_the_producers_handoff_objective() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"output": f"{member.role} done"}

    writer = _m("writer", handoff_objective="review the draft for accuracy")
    critic = _m("critic", ["writer"], subgoal="judge the work")
    res = await run_team(_team([writer, critic]), dispatch)
    # the writer→critic edge carries the writer's handoff task, NOT the critic's static subgoal
    assert _edge(res, "writer", "critic").objective_slice == "review the draft for accuracy"


async def test_acyclic_falls_back_to_subgoal_without_a_handoff_objective() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"output": "ok"}

    writer = _m("writer")  # no handoff_objective
    critic = _m("critic", ["writer"], subgoal="judge the work")
    res = await run_team(_team([writer, critic]), dispatch)
    # back-compat: with no per-edge task, the consumer's own subgoal stands
    assert _edge(res, "writer", "critic").objective_slice == "judge the work"
