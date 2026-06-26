"""ADR-043 #553 (slice 2/3) — bounded RECALIBRATION: a stalled loop diagnoses (CODED, external) and
emits ONE directive from a CLOSED action set before halting, then resumes over failed+blocked.
Bounded so recalibration can't become a second endless loop: a hard cap, a no-improvement stop (a
coded external delta — never the model's self-grade), and an anti-repeat guard.

RED until run_loop_seam grows ``recalibrate`` — imported function-locally so the module collects.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMLoop, OHMMember

pytestmark = pytest.mark.unit


def _m(role: str) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1")


def _loop(*roles: str) -> OHMLoop:
    return OHMLoop(members=list(roles), routing={r: f"do {r}" for r in roles})


def _by(*roles: str) -> dict[str, OHMMember]:
    return {r: _m(r) for r in roles}


def _directive(action: str, *targets: str):
    # function-local: RecalDirective is the #553 type, not built yet — keep collection clean
    from oraclous_ohm.orchestrate import RecalDirective

    return RecalDirective(action=action, reason="t", member_targets=list(targets))


async def _seam(**kw: Any):
    from oraclous_ohm.orchestrate import run_loop_seam

    return await run_loop_seam(**kw)


async def test_recalibration_fires_and_recovers() -> None:
    # b stalls on "v1" (the signature repeats) → one recalibration re-routes to b → it produces "v2"
    # → the coded done-check confirms. recalibration_used == 1; the breadcrumb never surfaces.
    bn = {"n": 0}

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "b":
            bn["n"] += 1
            return {"out": "v2" if bn["n"] >= 3 else "v1"}
        return {"out": "a"}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        if results.get("a") is None:
            return ["a"]
        return [] if results.get("b") == {"out": "v2"} else ["b"]

    async def done_check(results: dict[str, Any]) -> bool:
        return results.get("b") == {"out": "v2"}

    async def recalibrate(loop: OHMLoop, diag: Any):
        # the diagnosis is CODED + external — it reflects the loop's own state, not a self-grade
        assert diag.stall_kind in ("signature", "coordinator")
        return _directive("re-frame-objective", "b")

    res = await _seam(
        loop=_loop("a", "b"),
        by_role=_by("a", "b"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=20,
        recalibrate=recalibrate,
        recalibration_cap=2,
    )
    assert res.status == "converged"
    assert res.recalibrations_used == 1
    assert "__recalibration__" not in res.results  # the ephemeral breadcrumb never leaks out


def _const_stall():
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": "CONST"}  # identical every round → signature stall

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return ["a"]

    async def done_check(results: dict[str, Any]) -> bool:
        return False

    return dispatch, coordinate, done_check


async def test_recalibration_cap_halts() -> None:
    # a fresh directive every time, never converging → stops at the hard cap, not an endless loop
    d, c, dc = _const_stall()
    seq = {"n": 0}

    async def recalibrate(loop: OHMLoop, diag: Any):
        seq["n"] += 1
        return _directive("change-strategy", f"x{seq['n']}")  # distinct each time (no anti-repeat)

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=d,
        coordinate=c,
        done_check=dc,
        max_rounds=50,
        recalibrate=recalibrate,
        recalibration_cap=2,
    )
    assert res.recalibrations_used == 2  # exactly the cap, no more
    assert res.status in ("no_progress", "escalate")
    assert res.rounds < 50  # bounded — never burned the round cap


async def test_anti_repeat_escalates() -> None:
    # the SAME (action, targets) directive twice ⇒ escalate to a human, don't retry the same tack
    d, c, dc = _const_stall()

    async def recalibrate(loop: OHMLoop, diag: Any):
        return _directive("re-plan", "a")  # identical every call

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=d,
        coordinate=c,
        done_check=dc,
        max_rounds=50,
        recalibrate=recalibrate,
        recalibration_cap=3,
    )
    assert res.status == "escalate"
    assert res.recalibrations_used == 1  # applied once, then the repeat escalated


async def test_escalate_action_and_fail_closed_none() -> None:
    d, c, dc = _const_stall()

    async def recal_escalate(loop: OHMLoop, diag: Any):
        return _directive("escalate")  # the closed-set escalate action

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=d,
        coordinate=c,
        done_check=dc,
        max_rounds=50,
        recalibrate=recal_escalate,
        recalibration_cap=3,
    )
    assert res.status == "escalate"

    async def recal_none(loop: OHMLoop, diag: Any):
        return None  # unparseable / router unreachable → fail-closed halt

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=d,
        coordinate=c,
        done_check=dc,
        max_rounds=50,
        recalibrate=recal_none,
        recalibration_cap=3,
    )
    assert res.status == "no_progress"


async def test_no_recalibrator_is_unchanged_no_progress() -> None:
    # BACK-COMPAT: a loop with no recalibrator wired halts at no_progress exactly as #552 did
    d, c, dc = _const_stall()
    res = await _seam(
        loop=_loop("a"), by_role=_by("a"), dispatch=d, coordinate=c, done_check=dc, max_rounds=50
    )
    assert res.status == "no_progress"
    assert res.recalibrations_used == 0


async def test_recalibration_respects_the_runaway_bounds() -> None:
    # a recalibration cannot buy a free retry past a runaway bound — a blown wall-clock still halts
    d, c, dc = _const_stall()

    async def recalibrate(loop: OHMLoop, diag: Any):
        return _directive("re-plan", "a")

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=d,
        coordinate=c,
        done_check=dc,
        max_rounds=100,
        max_wall_seconds=1.0,
        started_at=0.0,
        clock=lambda: 100.0,  # already past the wall budget
        recalibrate=recalibrate,
        recalibration_cap=3,
    )
    assert res.status == "wall_time"  # the bound wins over recalibration (checked first each round)


async def test_resume_preserves_recalibration_count_and_digest() -> None:
    # resume across a HITL pause: the cap + anti-repeat survive (count + last digest are seeded)
    d, c, dc = _const_stall()

    async def recalibrate(loop: OHMLoop, diag: Any):
        return _directive("re-plan", "a")  # same digest as the seeded prior directive

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=d,
        coordinate=c,
        done_check=dc,
        max_rounds=50,
        recalibrate=recalibrate,
        recalibration_cap=3,
        resume_recalibrations_used=1,
        resume_last_directive_digest="re-plan|a",
    )
    # the prior digest matches this directive ⇒ anti-repeat escalates immediately on resume
    assert res.status == "escalate"
