"""ADR-043 #552 PR-B2 — the pure OHM seams the hybrid driver builds on.

``skeleton_members`` / ``loop_roles`` split a team into its acyclic skeleton + its genuine loop SCCs
(the importer already isolated the loops into ``orchestration.loops``); ``run_team(members=...)``
lets the hybrid driver run ONLY the condensed skeleton (the loops are interleaved as single nodes)
without mutating the authoritative ``manifest.members``. RED until the seams land.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import (
    OHMLoop,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRuntime,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=deps or [])


def _team(members: list[OHMMember], loops: list[OHMLoop] | None = None) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        orchestration=OHMOrchestration(loops=loops or []),
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _loop(*roles: str) -> OHMLoop:
    return OHMLoop(members=list(roles), routing={r: f"do {r}" for r in roles})


def test_loop_roles_is_the_union_of_every_loop_scc() -> None:
    team = _team([_m("a"), _m("w"), _m("c"), _m("p")], [_loop("w", "c")])
    assert team.loop_roles() == {"w", "c"}


def test_skeleton_members_excludes_loop_roles() -> None:
    team = _team([_m("intake"), _m("w"), _m("c"), _m("publish")], [_loop("w", "c")])
    assert [m.role for m in team.skeleton_members()] == ["intake", "publish"]


def test_acyclic_team_has_no_loop_roles_and_full_skeleton() -> None:
    # back-compat: a purely acyclic team's skeleton IS its members; loop_roles is empty
    team = _team([_m("a"), _m("b", ["a"])])
    assert team.loop_roles() == set()
    assert [m.role for m in team.skeleton_members()] == ["a", "b"]


async def test_run_team_members_param_runs_only_the_passed_subset() -> None:
    from oraclous_ohm.orchestrate import run_team

    seen: list[str] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(member.role)
        return {"out": member.role}

    team = _team([_m("a"), _m("b"), _m("c")])
    # pass only a subset — the excluded member 'c' must NOT be dispatched
    res = await run_team(team, dispatch, members=[team.members[0], team.members[1]])
    assert sorted(seen) == ["a", "b"]
    assert "c" not in res.results


async def test_run_team_members_none_is_unchanged_back_compat() -> None:
    from oraclous_ohm.orchestrate import run_team

    seen: list[str] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(member.role)
        return {"out": member.role}

    team = _team([_m("a"), _m("b", ["a"])])
    res = await run_team(team, dispatch)  # members=None → every member (existing behaviour)
    assert sorted(seen) == ["a", "b"]
    assert res.status == "completed"
