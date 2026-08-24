"""#828 + #832 — what the DAG driver must tell its caller while a stage is in flight.

``run_team`` fires ``on_checkpoint`` once per settled member (#819) and ``on_child`` once per
dispatched execution. Neither carries what a run view needs:

* nothing fires when a member is DISPATCHED, so the engine cannot write "this member is working
  right now" — only "this member finished";
* ``on_child`` receives a bare execution id, so the engine cannot map that execution back to the
  member that produced it, even though ``member.role`` is in scope at the call site.

#832 lands on the same seam and pulls the other way. ``_bounded`` fires the checkpoint OUTSIDE the
stage semaphore, so up to ``_STAGE_CONCURRENCY`` members can be inside the hook at once, each
holding its own snapshot. Nothing orders the snapshot against the emit, so an older snapshot can be
delivered last and the caller's row regresses. Adding a dispatch-time write roughly doubles the
emit rate on that path, which is why the two are fixed together.

The ordering property is asserted at THIS seam rather than only at the repository, because the
repository's row lock cannot fix it: by the time two writes queue for the same lock, the snapshots
they carry were already taken out of order.

RED until ``on_dispatch``, the role on ``on_child``, and the emit lock land.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime
from oraclous_ohm.orchestrate import run_team

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, *, depends_on: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=depends_on or [],
    )


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _dispatch_factory(
    *,
    ids: dict[str, str] | None = None,
    delays: dict[str, float] | None = None,
    on_child: Any = None,
) -> Any:
    """A dispatch that records its own execution id per role, exactly as the engine's does."""
    ids = ids if ids is not None else {}
    delays = delays or {}

    async def dispatch(
        member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any = None
    ) -> dict[str, Any]:
        if delays.get(member.role):
            await asyncio.sleep(delays[member.role])
        execution_id = ids.setdefault(member.role, str(uuid.uuid4()))
        if on_child is not None:
            on_child(execution_id, member.role)
        return {"id": execution_id, "status": "SUCCEEDED", "output": f"{member.role}-out"}

    return dispatch


# ── item 1: the driver announces a dispatch ──────────────────────────────────────────────────────


async def test_on_dispatch_fires_once_per_member_before_it_runs() -> None:
    order: list[str] = []

    async def dispatch(
        member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any = None
    ) -> dict[str, Any]:
        order.append(f"run:{member.role}")
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "x"}

    async def on_dispatch(role: str) -> None:
        order.append(f"dispatch:{role}")

    await run_team(
        _team([_m("a"), _m("b", depends_on=["a"])]),
        dispatch,
        on_dispatch=on_dispatch,
    )

    assert order == ["dispatch:a", "run:a", "dispatch:b", "run:b"]


async def test_on_dispatch_fires_inside_the_stage_semaphore() -> None:
    # The hook must mean "a dispatch slot is held", not "this member is queued somewhere behind
    # three others". With a stage wider than the concurrency cap, announcing at queue time would
    # report every member as working at once and the run view would lie about the whole stage.
    from oraclous_ohm.orchestrate import _STAGE_CONCURRENCY

    width = _STAGE_CONCURRENCY + 2
    announced: list[str] = []
    release = asyncio.Event()
    # LIVE count, not cumulative: on_dispatch raises it, the end of the member's dispatch lowers it
    # again. An append-only tally cannot express this assertion — announcing before dispatch is
    # required by the test above, so the running total necessarily passes the cap on a wide stage
    # no matter how correct the driver is. Only the number announced-and-not-yet-finished is
    # bounded by a dispatch slot, and that is the number this test is about.
    live: dict[str, int] = {"now": 0, "peak": 0}

    async def dispatch(
        member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any = None
    ) -> dict[str, Any]:
        if live["now"] >= _STAGE_CONCURRENCY:
            release.set()
        await release.wait()
        live["now"] -= 1
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "x"}

    async def on_dispatch(role: str) -> None:
        announced.append(role)
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])

    await run_team(
        _team([_m(f"m{i}") for i in range(width)]),
        dispatch,
        on_dispatch=on_dispatch,
    )

    assert sorted(announced) == sorted(f"m{i}" for i in range(width))  # every member, eventually
    assert live["peak"] <= _STAGE_CONCURRENCY, "announced more members than could be in flight"


async def test_a_raising_on_dispatch_hook_does_not_abort_the_run() -> None:
    # Same posture as on_checkpoint: the signal is best-effort. Losing it costs a run view some
    # resolution; letting it kill the drive costs the whole DAG.
    async def on_dispatch(role: str) -> None:
        raise RuntimeError("connection reset by peer")

    res = await run_team(
        _team([_m("a"), _m("b", depends_on=["a"])]),
        _dispatch_factory(),
        on_dispatch=on_dispatch,
    )

    assert res.member_status == {"a": "succeeded", "b": "succeeded"}


async def test_on_dispatch_does_not_fire_for_a_member_seeded_via_completed() -> None:
    # A resumed run (past a human gate, or a re-run) seeds already-finished members via
    # ``completed`` — they are REUSED, never dispatched. Announcing one as "running" is a lie: on
    # the engine side it overwrites that member's REAL historical timing with a fresh, near-zero
    # one (reported against PR #859 after the review round that added the D3/status_lock fixes).
    announced: list[str] = []

    async def on_dispatch(role: str) -> None:
        announced.append(role)

    res = await run_team(
        _team([_m("a"), _m("b", depends_on=["a"])]),
        _dispatch_factory(),
        on_dispatch=on_dispatch,
        completed={"a": {"id": "prior", "status": "SUCCEEDED", "output": "a-out"}},
    )

    assert announced == ["b"], "a seeded/reused member must not be announced as dispatched"
    assert res.member_status == {"a": "succeeded", "b": "succeeded"}


# ── item 4: the role rides along with the execution id ───────────────────────────────────────────


async def test_on_child_carries_the_role_that_produced_the_execution() -> None:
    seen: list[tuple[str, str]] = []
    ids: dict[str, str] = {}

    await run_team(
        _team([_m("researcher"), _m("writer", depends_on=["researcher"])]),
        _dispatch_factory(ids=ids),
        on_child=lambda execution_id, role: seen.append((execution_id, role)),
    )

    assert dict((role, eid) for eid, role in seen) == ids
    assert {role for _, role in seen} == {"researcher", "writer"}


# ── #832: the caller's snapshot never goes backwards ─────────────────────────────────────────────


async def test_checkpoint_snapshots_are_delivered_in_the_order_they_were_taken() -> None:
    # The race, forced. Two members of one stage settle, and the FIRST one's hook is made slow so
    # its older, smaller snapshot would arrive last. The caller persists whatever it is handed, so
    # an out-of-order delivery is a row that loses a member it already held.
    delivered: list[dict[str, Any]] = []
    first_emit = {"seen": False}

    async def on_checkpoint(results: dict[str, Any], member_status: dict[str, str]) -> None:
        if not first_emit["seen"]:
            first_emit["seen"] = True
            await asyncio.sleep(0.05)  # the first snapshot takes the long road to the row
        delivered.append(dict(results))

    await run_team(
        _team([_m("a"), _m("b"), _m("c")]),  # one stage, three concurrent members
        _dispatch_factory(delays={"a": 0.0, "b": 0.01, "c": 0.02}),
        on_checkpoint=on_checkpoint,
    )

    assert delivered, "no checkpoint was delivered at all"
    sizes = [len(snapshot) for snapshot in delivered]
    assert sizes == sorted(sizes), f"a snapshot shrank between deliveries: {sizes}"
    assert set(delivered[-1]) == {"a", "b", "c"}


async def test_the_emit_lock_is_not_the_stage_semaphore() -> None:
    # Serializing the emit must not cost a dispatch slot. If the ordering fix reuses the stage
    # semaphore, a slow checkpoint blocks the next member from being dispatched and a wide stage
    # silently degrades to a serial one — the throughput problem `_bounded` was shaped to avoid.
    from oraclous_ohm.orchestrate import _STAGE_CONCURRENCY

    if _STAGE_CONCURRENCY < 2:  # pragma: no cover — the cap is 4 by default
        pytest.skip("stage concurrency is capped at 1, so there is nothing to overlap")

    in_flight = {"now": 0, "peak": 0}

    async def dispatch(
        member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any = None
    ) -> dict[str, Any]:
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await asyncio.sleep(0.02)
        in_flight["now"] -= 1
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "x"}

    async def slow_checkpoint(results: dict[str, Any], member_status: dict[str, str]) -> None:
        await asyncio.sleep(0.05)  # slower than a dispatch, on purpose

    await run_team(
        _team([_m(f"m{i}") for i in range(_STAGE_CONCURRENCY)]),
        dispatch,
        on_checkpoint=slow_checkpoint,
    )

    assert in_flight["peak"] >= 2, "a slow checkpoint serialized the stage"
