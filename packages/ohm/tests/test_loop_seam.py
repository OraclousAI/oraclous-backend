"""run_loop_seam — the ADR-043 #552 bounded conductor seam for ONE loop SCC.

A genuine loop (a Tarjan SCC isolated at import; see test_import_assemble.py) runs round-by-round
through a bounded LLM-coordinator that ONLY PICKS the next member to run; every limit + the
done-check is CODED. The two load-bearing invariants this pins:

* **The team never satisfies its own done-check.** A round converges only when a CODED
  ``done_check`` (the engine's coverage-floor + landed-artifacts + separate-evaluator grade)
  confirms it — the
  coordinator's "I'm done" (``[]``) NEVER finishes the run on its own; if the coordinator gives up
  but the coded check disagrees, that is a stall (no-progress), not a success.
* **Four always-on runaway bounds** (max rounds / wall-clock / cost budget / no-progress), any one
  of which halts with a SAVED PARTIAL result — never an infinite loop, never a hard abort (#551
  non-abort: a member failure is recorded, the loop keeps going).

RED until run_loop_seam lands (imported function-locally so the module still collects).
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.errors import OHMError
from oraclous_ohm.manifest import OHMLoop, OHMMember

pytestmark = pytest.mark.unit


def _m(role: str) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1")


def _loop(*roles: str) -> OHMLoop:
    return OHMLoop(members=list(roles), routing={r: f"do {r}" for r in roles})


def _by(*roles: str) -> dict[str, OHMMember]:
    return {r: _m(r) for r in roles}


async def _seam(**kw: Any):
    # function-local import: run_loop_seam is the #552 seam not built yet — keep collection clean
    from oraclous_ohm.orchestrate import run_loop_seam

    return await run_loop_seam(**kw)


async def test_converges_only_when_the_coded_done_check_confirms() -> None:
    seen: list[str] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(member.role)
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        if "a" not in results:
            return ["a"]
        if "b" not in results:
            return ["b"]
        return []  # the coordinator believes the goal is met

    async def done_check(results: dict[str, Any]) -> bool:
        return "a" in results and "b" in results  # the CODED authority

    res = await _seam(
        loop=_loop("a", "b"),
        by_role=_by("a", "b"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=10,
    )
    assert res.status == "converged"
    assert res.results["a"] == {"out": "a"} and res.results["b"] == {"out": "b"}


async def test_coordinator_self_done_does_not_finish_when_coded_check_disagrees() -> None:
    # the coordinator says done immediately ([]), but the coded done_check never confirms — the team
    # CANNOT satisfy its own done-check, so this is a stall (no-progress), NOT a convergence.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return []  # "I'm done" — but the coded check below disagrees

    async def done_check(results: dict[str, Any]) -> bool:
        return False  # coverage/grade not met

    res = await _seam(
        loop=_loop("a", "b"),
        by_role=_by("a", "b"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=10,
    )
    assert res.status == "no_progress"  # NOT "converged" — the coded check overrides the model


async def test_max_rounds_bound_halts_with_a_saved_partial() -> None:
    calls = {"n": 0}

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        calls["n"] += 1
        return {"out": member.role, "round": calls["n"]}  # changes each round → not no-progress

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["a"]  # keep dispatching; never converge

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=3,
    )
    assert res.status == "max_rounds"
    assert res.rounds == 3
    assert res.results.get("a") is not None  # the partial result is saved, not discarded


async def test_wall_time_bound_halts_with_a_saved_partial() -> None:
    # an injected clock that jumps past the wall budget after the first round
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])

    def clock() -> float:
        return next(ticks)

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["a"]

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=100,
        max_wall_seconds=1.0,
        clock=clock,
    )
    assert res.status == "wall_time"
    assert res.results.get("a") is not None


async def test_cost_budget_bound_halts_with_a_saved_partial() -> None:
    spent = {"tokens": 0}

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        spent["tokens"] += 100
        return {"out": member.role, "spent": spent["tokens"]}  # changes each round → has progress

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["a"]

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=100,
        max_cost=250,
        cost_so_far=lambda: spent["tokens"],
    )
    assert res.status == "cost_budget"  # trips once the accumulated cost passes the budget
    assert res.results.get("a") is not None


async def test_no_progress_bound_halts() -> None:
    # the loop dispatches but nothing changes round-over-round (same output, no status change) and
    # the coded check never confirms — a no-progress stall, halted (not an infinite loop).
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": "CONSTANT"}  # identical every round → no new information lands

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["a"]

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=50,
    )
    assert res.status == "no_progress"
    assert res.rounds < 50  # it stopped well before the round cap


async def test_member_failure_in_the_loop_is_non_abort() -> None:
    # #551 non-abort: a member's dispatch raising is RECORDED (member_status/errors) and the loop
    # keeps running its other members — it never raises out of the seam.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "b":
            raise RuntimeError("b harness boom")
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["a", "b"]

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    res = await _seam(
        loop=_loop("a", "b"),
        by_role=_by("a", "b"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=2,
    )
    assert res.member_status.get("a") == "succeeded"
    assert res.member_status.get("b") == "failed"  # recorded, not raised
    assert "boom" in res.member_errors.get("b", "")
    assert res.results.get("a") == {"out": "a"}  # the surviving member kept producing


async def test_coordinator_routes_only_to_declared_loop_members() -> None:
    # a fail-closed guardrail: the coordinator may route ONLY to members of THIS loop; a route to an
    # outsider (a skeleton member or an unknown role) raises rather than dispatching it.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["outsider"]  # not in the loop

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    with pytest.raises(OHMError):
        await _seam(
            loop=_loop("a", "b"),
            by_role=_by("a", "b"),
            dispatch=dispatch,
            coordinate=coordinate,
            done_check=done_check,
            max_rounds=5,
        )
