"""Blocking human-gate nodes in the team-DAG orchestrator (#422; ADR-035 §6).

A ``kind: human`` member pauses the run until the author advances it; downstream members cannot run
until it is approved (agents cannot cross). The durable task_service persist/resume is the wiring
follow-up — this proves the gate SEMANTICS (block / approve-continue / reject-halt) in the core.
"""

from __future__ import annotations

import uuid
from typing import Any

from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime
from oraclous_ohm.orchestrate import run_team

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _agent(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=deps or [])


def _gate(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="human", human_role="author", depends_on=deps or [])


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def _dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
    return {"ran": member.role}


def _gated_team() -> OHMManifest:
    return _team([_agent("a"), _gate("gate-b", ["a"]), _agent("c", ["gate-b"])])


async def test_undecided_gate_pauses_and_blocks_downstream() -> None:
    res = await run_team(_gated_team(), _dispatch)  # no gate_decisions
    assert res.status == "paused"
    assert res.paused_at == ["gate-b"]
    assert "a" in res.results  # the upstream agent ran
    assert "c" not in res.results  # downstream BLOCKED — agents cannot cross the gate


async def test_dispatch_never_called_past_an_undecided_gate() -> None:
    ran: list[str] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        ran.append(member.role)
        return {}

    await run_team(_gated_team(), dispatch)
    assert "a" in ran and "c" not in ran  # c was never dispatched (no side effect past the gate)


async def test_approved_gate_lets_downstream_run() -> None:
    res = await run_team(_gated_team(), _dispatch, gate_decisions={"gate-b": "approve"})
    assert res.status == "completed"
    assert res.results["c"] == {"ran": "c"}
    assert res.results["gate-b"]["decision"] == "approve"


async def test_rejected_gate_halts_the_run() -> None:
    res = await run_team(_gated_team(), _dispatch, gate_decisions={"gate-b": "reject"})
    assert res.status == "rejected"
    assert res.paused_at == ["gate-b"]
    assert "c" not in res.results


async def test_approved_gate_threads_its_decision_downstream() -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        captured.append((member.role, {e.from_role: e.payload for e in envs}))
        return {"ran": member.role}

    await run_team(_gated_team(), dispatch, gate_decisions={"gate-b": "approve"})
    c_envs = next(envs for role, envs in captured if role == "c")
    assert "gate-b" in c_envs  # c received the gate's decision as a hand-off
    assert c_envs["gate-b"]["decision"] == "approve"
