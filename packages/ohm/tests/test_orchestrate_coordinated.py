"""B2 — the prose-interpreting orchestration agent (#424; ADR-035, OPT-IN).

``run_team_coordinated`` lets an injected coordinator (an LLM in the runtime) route the team instead
of the fixed DAG — "choice is prose, mechanics are coded". These pin the CODED mechanics that the
prose can never override: the coordinator may route ONLY to declared members (fail-closed), and the
loop is bounded by a coded termination. A deterministic fake coordinator stands in for the LLM.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.errors import OHMError
from oraclous_ohm.manifest import (
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRuntime,
    OHMTermination,
)
from oraclous_ohm.orchestrate import run_team_coordinated

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, depends_on: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=depends_on or []
    )


def _team(members: list[OHMMember], orchestration: OHMOrchestration | None = None) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        orchestration=orchestration,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def _ok_dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
    return {"r": member.role, "saw": [e.from_role for e in envs]}


async def test_coordinator_routes_the_team_until_it_declares_done() -> None:
    # the coordinator picks a -> b, then declares the goal met ([]). b sees a's hand-off.
    plan = [["a"], ["b"], []]

    async def coordinate(brief: OHMOrchestration, results: dict, remaining: list[str]) -> list[str]:
        return plan.pop(0) if plan else []

    res = await run_team_coordinated(_team([_m("a"), _m("b", ["a"])]), _ok_dispatch, coordinate)
    assert set(res.results) == {"a", "b"}
    assert res.results["b"]["saw"] == ["a"]  # the hand-off threaded to the coordinator's pick


async def test_coordinator_cannot_route_to_an_undeclared_member() -> None:
    # the R4 T3-M1 guardrail: a prose route to a member the manifest never declared is fail-closed.
    async def coordinate(brief: OHMOrchestration, results: dict, remaining: list[str]) -> list[str]:
        return ["ghost"]  # not a declared member

    with pytest.raises(OHMError):
        await run_team_coordinated(_team([_m("a")]), _ok_dispatch, coordinate)


async def test_a_non_converging_coordinator_is_bounded_by_termination() -> None:
    # a coordinator that NEVER declares done must not run away — the coded max_rounds bounds it.
    calls = 0

    async def coordinate(brief: OHMOrchestration, results: dict, remaining: list[str]) -> list[str]:
        nonlocal calls
        calls += 1
        return ["a"]  # always re-route; never converges

    res = await run_team_coordinated(_team([_m("a")]), _ok_dispatch, coordinate, max_rounds=3)
    assert res.status == "completed"
    assert calls == 3  # stopped at the coded bound, did not loop forever


async def test_orchestration_termination_max_rounds_caps_the_loop() -> None:
    # the manifest's orchestration.termination.max_rounds also caps the loop (the tighter wins).
    async def coordinate(brief: OHMOrchestration, results: dict, remaining: list[str]) -> list[str]:
        return ["a"]

    brief = OHMOrchestration(termination=OHMTermination(max_rounds=2))
    res = await run_team_coordinated(
        _team([_m("a")], orchestration=brief), _ok_dispatch, coordinate, max_rounds=100
    )
    assert res.status == "completed"  # bounded by the manifest's max_rounds=2, not the 100 arg
