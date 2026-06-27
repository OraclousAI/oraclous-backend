"""ADR-043 #552 PR-B2 — the HYBRID driver: the acyclic skeleton runs on ``run_team`` and each
genuine loop SCC runs the bounded ``run_loop_seam`` conductor, interleaved at its topological
position (a downstream skeleton member runs only AFTER the loop it depends on converges). The
coordinator (picks the next loop member) + the coded done-check are INJECTED; a loop team with
either unwired FAILS CLOSED. A non-converged loop is non-abort + re-runnable (#551).

RED until ``run_team_hybrid`` lands — imported function-locally so the module still collects.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.errors import OHMError
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
    """Records each member dispatch (by manifest_ref role) and always succeeds."""

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


def _loop(*roles: str) -> OHMLoop:
    return OHMLoop(members=list(roles), routing={r: f"do {r}" for r in roles})


def _team(members: list[OHMMember], loops: list[OHMLoop], max_rounds: int = 5) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        orchestration=OHMOrchestration(
            loops=loops, termination=OHMTermination(max_rounds=max_rounds)
        ),
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _coordinate_until_all_produced():
    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return [r for r in loop.members if results.get(r) is None]

    return coordinate


def _done_when_all_produced(loop: OHMLoop, diag: dict[str, Any] | None = None):
    async def done(results: dict[str, Any]) -> bool:
        return all(results.get(r) is not None for r in loop.members)

    return done


async def _hybrid(manifest: OHMManifest, harness: Any, **kw: Any):
    from oraclous_execution_engine_service.services.team_run import run_team_hybrid

    return await run_team_hybrid(manifest, harness, **kw)


async def test_pure_loop_converges_and_drops_the_internal_node() -> None:
    h = _FakeHarness()
    res = await _hybrid(
        _team([_m("w"), _m("c")], [_loop("w", "c")]),
        h,
        coordinate=_coordinate_until_all_produced(),
        done_check_for=_done_when_all_produced,
    )
    assert res.status == "completed"
    assert set(res.results) == {"w", "c"}  # the loop members merged in
    assert not any(r.startswith("__loop__") for r in res.results)  # internal node hidden


async def test_downstream_skeleton_member_runs_after_the_loop() -> None:
    # intake -> (w<->c) -> publish : the conductor must order publish AFTER the loop converges
    h = _FakeHarness()
    mf = _team(
        [_m("intake"), _m("w", ["intake"]), _m("c"), _m("publish", ["w"])], [_loop("w", "c")]
    )
    res = await _hybrid(
        mf, h, coordinate=_coordinate_until_all_produced(), done_check_for=_done_when_all_produced
    )
    assert res.status == "completed"
    assert h.calls[0] == "intake"  # upstream skeleton first
    assert h.calls[-1] == "publish"  # downstream skeleton LAST (after the loop)
    assert {"intake", "w", "c", "publish"} <= set(res.results)


async def test_non_converged_loop_is_failed_re_runnable_and_blocks_downstream() -> None:
    h = _FakeHarness()
    mf = _team([_m("w"), _m("c"), _m("publish", ["w"])], [_loop("w", "c")], max_rounds=2)

    def never(loop: OHMLoop, diag: dict[str, Any] | None = None):
        async def done(results: dict[str, Any]) -> bool:
            return False

        return done

    res = await _hybrid(mf, h, coordinate=_coordinate_until_all_produced(), done_check_for=never)
    assert res.status == "failed"
    assert res.member_status.get("w") == "failed"  # loop members re-runnable (ADR-042)
    assert res.member_status.get("c") == "failed"
    assert res.member_status.get("publish") == "blocked"  # downstream blocked, never dispatched


async def test_loops_present_but_no_coordinator_fails_closed() -> None:
    with pytest.raises(OHMError):
        await _hybrid(
            _team([_m("w"), _m("c")], [_loop("w", "c")]),
            _FakeHarness(),
            coordinate=None,
            done_check_for=_done_when_all_produced,
        )


async def test_acyclic_team_is_unchanged_passthrough() -> None:
    # no loops → the single-pass DAG path (coordinator/done-check irrelevant)
    h = _FakeHarness()
    mf = _team([_m("a"), _m("b", ["a"])], [])
    res = await _hybrid(mf, h, coordinate=None, done_check_for=None)
    assert res.status == "completed"
    assert h.calls == ["a", "b"]


async def test_cost_so_far_reads_live_spend_for_the_loop_bound() -> None:
    # the loop cost bound must see the LIVE accumulated spend (skeleton + loop), not a snapshot
    seen_costs: list[int] = []
    spend = {"n": 0}

    class _Costing(_FakeHarness):
        async def execute(self, **kw: Any) -> dict[str, Any]:
            spend["n"] += 100
            return await super().execute(**kw)

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        seen_costs.append(cost())  # capture what the bound sees each round
        return [r for r in loop.members if results.get(r) is None]

    def cost() -> int:
        return spend["n"]

    h = _Costing()
    await _hybrid(
        _team([_m("w"), _m("c")], [_loop("w", "c")]),
        h,
        coordinate=coordinate,
        done_check_for=_done_when_all_produced,
        cost_so_far=cost,
    )
    # the coordinator saw the live (growing) spend, not a constant snapshot
    assert seen_costs == sorted(seen_costs)
