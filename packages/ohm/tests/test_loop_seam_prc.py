"""ADR-043 #552 PR-C (step 6) — the conductor seam gains: WITHIN-ROUND de-dup, a per-round HUMAN
GATE (pause before a round; no auto-skip), and CHECKPOINT/RESUME at a round boundary (resume the
round counter + the ORIGINAL wall-clock origin, not a fresh budget). The four runaway bounds still
WIN over a pending gate (the always-on safety net). RED until run_loop_seam grows the new params.

Imported function-locally so the module collects on main (where the new params don't exist yet).
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMLoop, OHMMember

pytestmark = pytest.mark.unit


def _m(role: str) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1")


def _gate(role: str) -> OHMMember:
    return OHMMember(role=role, kind="human", human_role="approver")


def _loop(*roles: str) -> OHMLoop:
    return OHMLoop(members=list(roles), routing={r: f"do {r}" for r in roles})


async def _seam(**kw: Any):
    from oraclous_ohm.orchestrate import run_loop_seam

    return await run_loop_seam(**kw)


async def test_within_round_duplicate_pick_dispatches_once() -> None:
    # a duplicate pick WITHIN one round dispatches the member once (a cross-round re-visit is fine,
    # but a round must not double-dispatch + double-charge a single member).
    seen: list[str] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(member.role)
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["a", "a", "b"] if not results else []

    async def done_check(results: dict[str, Any]) -> bool:
        return "a" in results and "b" in results

    res = await _seam(
        loop=_loop("a", "b"),
        by_role={"a": _m("a"), "b": _m("b")},
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=5,
    )
    assert res.status == "converged"
    assert seen.count("a") == 1  # the duplicate pick of 'a' dispatched ONCE this round


async def test_per_round_human_gate_pauses_before_any_dispatch() -> None:
    # a kind:human loop member with NO decision halts the loop BEFORE the round — no auto-skip, no
    # agent dispatched, rounds not incremented (the gate can never be silently crossed).
    dispatched: list[str] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        dispatched.append(member.role)
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        raise AssertionError("the coordinator must not be consulted while a gate is undecided")

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    res = await _seam(
        loop=_loop("gate", "writer"),
        by_role={"gate": _gate("gate"), "writer": _m("writer")},
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=5,
        gate_decisions={},  # the gate is undecided
    )
    assert res.status == "paused"
    assert res.paused_at == ["gate"]
    assert res.rounds == 0  # no round consumed
    assert dispatched == []  # nothing ran — the gate was honoured


async def test_resume_with_gate_decided_proceeds_and_converges() -> None:
    # once the human approves, the resumed seam dispatches the round; the gate member is RENDERED
    # (its decision recorded), never harness-dispatched.
    dispatched: list[str] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        dispatched.append(member.role)
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["writer"] if "writer" not in results else []

    async def done_check(results: dict[str, Any]) -> bool:
        return "writer" in results

    res = await _seam(
        loop=_loop("gate", "writer"),
        by_role={"gate": _gate("gate"), "writer": _m("writer")},
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=5,
        gate_decisions={"gate": "approve"},  # the human approved
    )
    assert res.status == "converged"
    assert dispatched == ["writer"]  # the gate was NOT harness-dispatched, only the agent ran
    assert res.results["gate"] == {"gate": "gate", "decision": "approve"}


async def test_resume_from_round_continues_the_round_counter() -> None:
    # resuming at round 3 of a max-5 loop may only run 2 more rounds (the cap accounts for the
    # already-spent rounds — a resume cannot buy a fresh full round budget).
    tick = {"n": 0}

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        tick["n"] += 1
        return {"out": member.role, "n": tick["n"]}  # changing output → not a no-progress stall

    rounds_seen: list[int] = []

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        rounds_seen.append(rounds_left)
        return ["a"]  # never converge

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    res = await _seam(
        loop=_loop("a"),
        by_role={"a": _m("a")},
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=5,
        resume_from_round=3,
    )
    assert res.status == "max_rounds"
    assert res.rounds == 5  # ran from 3 → 5, not 0 → 5
    assert len(rounds_seen) == 2  # only 2 rounds left after a resume at 3


async def test_wall_clock_measured_from_original_start_on_resume() -> None:
    # the LOAD-BEARING fix: a resumed loop measures wall-clock from the ORIGINAL run start, so a
    # long pause does NOT hand the resume a fresh timeout. With started_at=0 and a clock past the
    # budget, the first resumed iteration trips wall_time.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["a"]

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    res = await _seam(
        loop=_loop("a"),
        by_role={"a": _m("a")},
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=100,
        max_wall_seconds=1.0,
        started_at=0.0,  # the ORIGINAL run start (persisted across the pause)
        clock=lambda: 100.0,  # already far past the 1.0s budget
    )
    assert res.status == "wall_time"  # NOT reset to a fresh budget on resume


async def test_a_runaway_bound_wins_over_an_undecided_gate() -> None:
    # the four always-on bounds are the outermost safety net: a loop that has blown its wall-clock
    # budget HALTS (saved partial) rather than parking forever on a gate nobody will approve.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["writer"]

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    res = await _seam(
        loop=_loop("gate", "writer"),
        by_role={"gate": _gate("gate"), "writer": _m("writer")},
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=100,
        max_wall_seconds=1.0,
        started_at=0.0,
        clock=lambda: 100.0,  # the wall-clock bound is already tripped
        gate_decisions={},  # ...and the gate is undecided
    )
    assert res.status == "wall_time"  # the bound wins, not "paused"
