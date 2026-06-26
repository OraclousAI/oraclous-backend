"""ADR-043 #552 PR-C — the hybrid driver threads the per-round HITL gate + the loop checkpoint into
run_loop_seam: a loop with an UNDECIDED kind:human gate pauses the whole team (status="paused",
the gate in paused_at, members NOT marked failed); a DECIDED gate resumes + converges; the loop's
round/started_at checkpoint (``loop_state``) round-trips so a resume continues at a round boundary.

RED until run_team_hybrid threads gate_decisions to the seam + carries loop_state.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.manifest import (
    OHMLoop,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRuntime,
    OHMTermination,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


class _FakeHarness:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        manifest_ref: str | None = None,
        capability_ceiling: list[str] | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        role = (manifest_ref or "?/?").split("/")[-1].split("@")[0]
        self.calls.append(role)
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": f"{role}-out"}


def _m(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=deps or [])


def _gate(role: str) -> OHMMember:
    return OHMMember(role=role, kind="human", human_role="approver")


def _loop(*roles: str) -> OHMLoop:
    return OHMLoop(members=list(roles), routing={r: f"do {r}" for r in roles})


def _team(members: list[OHMMember], loops: list[OHMLoop]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        orchestration=OHMOrchestration(loops=loops, termination=OHMTermination(max_rounds=5)),
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _coord_until_all():
    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return [r for r in loop.members if results.get(r) is None]

    return coordinate


def _done_all(loop: OHMLoop):
    async def done(results: dict[str, Any]) -> bool:
        return all(results.get(r) is not None for r in loop.members if r != "gate")

    return done


async def _hybrid(manifest: OHMManifest, harness: Any, **kw: Any):
    from oraclous_execution_engine_service.services.team_run import run_team_hybrid

    return await run_team_hybrid(manifest, harness, **kw)


async def test_undecided_gate_pauses_the_team_not_fails_it() -> None:
    h = _FakeHarness()
    mf = _team([_gate("gate"), _m("writer")], [_loop("gate", "writer")])
    res = await _hybrid(
        mf,
        h,
        coordinate=_coord_until_all(),
        done_check_for=_done_all,
        gate_decisions={},  # the gate is undecided
    )
    assert res.status == "paused"
    assert "gate" in res.paused_at
    # a pause is NOT a failure — the loop members must not be marked failed/blocked
    assert not any(s in ("failed", "blocked") for s in res.member_status.values())
    assert h.calls == []  # nothing dispatched while the gate is undecided


async def test_decided_gate_resumes_and_converges() -> None:
    h = _FakeHarness()
    mf = _team([_gate("gate"), _m("writer")], [_loop("gate", "writer")])
    res = await _hybrid(
        mf,
        h,
        coordinate=_coord_until_all(),
        done_check_for=_done_all,
        gate_decisions={"gate": "approve"},  # the human approved
    )
    assert res.status == "completed"
    assert "writer" in res.results
    assert h.calls == ["writer"]  # the gate rendered (not harness-dispatched), only the agent ran


async def test_loop_state_checkpoint_round_trips() -> None:
    # the hybrid accepts a prior loop_state (resume_from_round + started_at) and returns the updated
    # checkpoint so _drive can persist it across drives.
    h = _FakeHarness()
    mf = _team([_m("w"), _m("c")], [_loop("w", "c")])
    res = await _hybrid(
        mf,
        h,
        coordinate=_coord_until_all(),
        done_check_for=_done_all,
        loop_state={},  # fresh run
    )
    assert res.status == "completed"
    assert res.loop_state, "the hybrid must surface the loop checkpoint (round/started_at/status)"
    cp = res.loop_state["0"]  # the first (only) loop
    assert cp["round"] >= 1 and "started_at" in cp and cp["status"] == "converged"
