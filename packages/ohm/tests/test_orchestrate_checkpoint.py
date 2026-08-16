"""#819 — the orchestrator emits a CHECKPOINT each time a member settles, so a drive that is killed
mid-run leaves the finished members durable.

Today ``run_team`` accumulates ``results`` + ``member_status`` in memory and returns them ONCE, at
the end. The engine therefore has nothing to persist until the whole DAG finishes, and a Celery
soft-time-limit kill at 50 minutes throws away every member's output — including the ones that
finished forty minutes earlier (issue #819).

These tests pin the seam that closes it: an optional ``on_checkpoint`` coroutine, called with a
SNAPSHOT of ``(results, member_status)`` every time a member reaches a terminal status. The engine
wires it to a durable write; the orchestrator stays a pure executor that knows nothing about a
database.

Two properties are load-bearing and easy to get wrong:

* **The snapshot is a COPY.** Handing the caller the live accumulator makes every checkpoint alias
  the same dict, so the last state overwrites the record of every earlier one and the whole feature
  silently does nothing.
* **A failed member checkpoints too.** ``/rerun`` targets the members whose status is "failed" or
  "blocked", so a checkpoint that only records successes cannot produce a re-runnable row.

RED until ``on_checkpoint`` lands on ``run_team`` / ``run_loop_seam``. ``run_team`` itself already
exists, so it is imported at module level; the not-yet-built ``run_loop_seam`` keyword is reached
through a function-local import in the loop tests.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import (
    OHMLoop,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMRuntime,
)
from oraclous_ohm.orchestrate import run_team

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, depends_on: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=depends_on or []
    )


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


class _Recorder:
    """Captures every checkpoint the orchestrator emits, in order."""

    def __init__(self) -> None:
        self.snapshots: list[tuple[dict[str, Any], dict[str, str]]] = []

    async def __call__(self, results: dict[str, Any], member_status: dict[str, str]) -> None:
        self.snapshots.append((results, member_status))

    @property
    def statuses(self) -> list[dict[str, str]]:
        return [s for _, s in self.snapshots]


# ── the checkpoint fires, once per settled member ────────────────────────────────────────────────


async def test_on_checkpoint_fires_once_per_settled_member() -> None:
    # Three members in a chain. Each one settling emits exactly one checkpoint, and the run's own
    # return value is unchanged — the hook OBSERVES, it never alters the result.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    rec = _Recorder()
    res = await run_team(
        _team([_m("a"), _m("b", ["a"]), _m("c", ["b"])]), dispatch, on_checkpoint=rec
    )

    assert len(rec.snapshots) == 3  # one per member, not one per stage and not one at the end
    assert rec.statuses[-1] == {"a": "succeeded", "b": "succeeded", "c": "succeeded"}
    assert res.member_status == rec.statuses[-1]  # the hook did not change the outcome
    assert res.results["c"] == {"out": "c"}


async def test_each_checkpoint_carries_only_the_members_settled_so_far() -> None:
    # The point of the feature: at the moment 'a' settles the row must already be able to record
    # 'a' ALONE. A hook that only ever sees the finished state is worth nothing to a killed drive.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    rec = _Recorder()
    await run_team(_team([_m("a"), _m("b", ["a"])]), dispatch, on_checkpoint=rec)

    assert rec.statuses[0] == {"a": "succeeded"}  # 'b' has not run yet
    assert rec.snapshots[0][0] == {"a": {"out": "a"}}  # ...and is absent from results too
    assert rec.statuses[1] == {"a": "succeeded", "b": "succeeded"}


async def test_checkpoint_snapshots_are_independent_copies() -> None:
    # THE defect this feature dies on. If the live accumulators are handed to the hook, every
    # snapshot aliases one dict: snapshot[0] mutates as later members land, the engine writes the
    # same end-state each time, and a killed drive still recovers nothing. Assert non-aliasing
    # directly rather than trusting that the implementation copied.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    rec = _Recorder()
    await run_team(_team([_m("a"), _m("b", ["a"])]), dispatch, on_checkpoint=rec)

    first_results, first_status = rec.snapshots[0]
    second_results, second_status = rec.snapshots[1]
    assert first_status is not second_status  # distinct objects, not one live dict
    assert first_results is not second_results
    assert "b" not in first_status  # the earlier snapshot did NOT grow when 'b' landed
    assert "b" not in first_results


async def test_a_failed_member_is_checkpointed_with_its_failed_status() -> None:
    # /rerun targets members whose status is "failed" or "blocked". A checkpoint that skips
    # failures cannot leave a re-runnable row, so the failure must reach the hook like any other
    # terminal status — and the run must not abort (#551 non-abort).
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "b":
            raise RuntimeError("b exploded")
        return {"out": member.role}

    rec = _Recorder()
    res = await run_team(_team([_m("a"), _m("b"), _m("c")]), dispatch, on_checkpoint=rec)

    assert res.status == "failed"
    assert rec.statuses[-1]["b"] == "failed"
    assert any("b" in s for s in rec.statuses)  # 'b' was checkpointed, not skipped
    # every member reached the hook, so the final checkpoint alone is a complete re-run target
    assert set(rec.statuses[-1]) == {"a", "b", "c"}


async def test_a_blocked_member_is_checkpointed_too() -> None:
    # A member whose dependency failed is BLOCKED and never dispatched. It is still a valid /rerun
    # target, so it must appear in a checkpoint — the hook cannot be wired to dispatch alone.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "a":
            raise RuntimeError("a exploded")
        return {"out": member.role}

    rec = _Recorder()
    res = await run_team(_team([_m("a"), _m("b", ["a"])]), dispatch, on_checkpoint=rec)

    assert res.member_status == {"a": "failed", "b": "blocked"}
    assert rec.statuses[-1] == {"a": "failed", "b": "blocked"}


async def test_checkpoints_survive_a_kill_mid_stage() -> None:
    # The #819 shape at orchestrator level. A wide stage where one member never returns: the drive
    # is cancelled from outside (Celery's SoftTimeLimitExceeded lands the same way — an exception
    # raised into the running task). The members that already settled must have been checkpointed
    # BEFORE the kill, which is only true if the hook fires per member rather than per stage.
    started = asyncio.Event()

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "slow":
            started.set()
            await asyncio.sleep(30)  # never completes; the kill arrives first
            raise AssertionError("unreachable")
        return {"out": member.role}

    rec = _Recorder()
    task = asyncio.create_task(
        run_team(_team([_m("quick"), _m("slow")]), dispatch, on_checkpoint=rec)
    )
    await started.wait()
    await asyncio.sleep(0.05)  # let 'quick' settle and checkpoint
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert rec.snapshots, "the killed drive checkpointed nothing — every member's output is lost"
    assert rec.statuses[-1].get("quick") == "succeeded"
    assert rec.snapshots[-1][0]["quick"] == {"out": "quick"}


async def test_run_team_without_on_checkpoint_is_unchanged() -> None:
    # The hook is optional: an orchestrator caller that does not want checkpoints (the pure unit
    # tests, the compiler) keeps today's behaviour byte for byte.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    res = await run_team(_team([_m("a"), _m("b", ["a"])]), dispatch)
    assert res.status == "completed"
    assert res.member_status == {"a": "succeeded", "b": "succeeded"}


async def test_a_raising_checkpoint_hook_does_not_abort_the_run() -> None:
    # Decision: durability is best-effort. A transient write failure mid-drive must never turn a
    # healthy run into a FAILED one — losing a checkpoint costs recovery granularity, losing the
    # run costs the whole DAG. Mirrors the existing best-effort `_accrue_schedule_cost` posture.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    async def exploding(results: dict[str, Any], member_status: dict[str, str]) -> None:
        raise RuntimeError("the database went away")

    res = await run_team(_team([_m("a"), _m("b", ["a"])]), dispatch, on_checkpoint=exploding)
    assert res.status == "completed"  # the run finished despite every checkpoint failing
    assert res.member_status == {"a": "succeeded", "b": "succeeded"}


# ── loop teams: the same hook, fired at each round boundary (#819 decision 4) ─────────────────────


def _loop(*roles: str) -> OHMLoop:
    return OHMLoop(members=list(roles), routing={r: f"do {r}" for r in roles})


def _by(*roles: str) -> dict[str, OHMMember]:
    return {r: _m(r) for r in roles}


async def _seam(**kw: Any):
    # function-local import: the ``on_checkpoint`` keyword is the #819 seam, so this keeps module
    # collection clean and fails the test at runtime instead (tests-seam-imports rule).
    from oraclous_ohm.orchestrate import run_loop_seam

    return await run_loop_seam(**kw)


async def test_loop_seam_checkpoints_each_round() -> None:
    # #819 decision 4: a loop team is the likeliest thing on the platform to run past fifty
    # minutes, so it gets the same durability as an acyclic one. The loop's natural sync point is
    # the round boundary, so the hook fires once per round with what that round produced.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        if "a" not in results:
            return ["a"]
        if "b" not in results:
            return ["b"]
        return []

    async def done_check(results: dict[str, Any]) -> bool:
        return "a" in results and "b" in results

    rec = _Recorder()
    res = await _seam(
        loop=_loop("a", "b"),
        by_role=_by("a", "b"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=10,
        on_checkpoint=rec,
    )

    assert res.status == "converged"
    assert len(rec.snapshots) >= 2  # at least one per productive round
    assert rec.statuses[0] == {"a": "succeeded"}  # round 1 durable before round 2 starts
    assert rec.statuses[-1] == {"a": "succeeded", "b": "succeeded"}
    assert rec.snapshots[0][0] is not rec.snapshots[-1][0]  # copies here too


async def test_loop_seam_without_on_checkpoint_is_unchanged() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"out": member.role}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return [] if "a" in results else ["a"]

    async def done_check(results: dict[str, Any]) -> bool:
        return "a" in results

    res = await _seam(
        loop=_loop("a"),
        by_role=_by("a"),
        dispatch=dispatch,
        coordinate=coordinate,
        done_check=done_check,
        max_rounds=5,
    )
    assert res.status == "converged" and res.results["a"] == {"out": "a"}
