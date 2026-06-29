"""#587 — a DEGRADED member (harness PARTIAL) surfaces as a flagged ``partial``, not a team failure.

When a member's loop exhausts its budget under ``on_exhaustion: degrade`` the harness returns a
PARTIAL result (its best-effort ``last_text``). The team orchestrator must record that member
``member_status == "partial"`` (a 6th terminal, distinct from ``succeeded``/``failed``), thread its
output downstream (it is NOT blocked), and NOT make the team verdict ``failed`` — a degrade is a
governed graceful exhaustion, never a crash. A real member FAILURE still outranks it (#585).

RED until the [impl] maps a dispatch ``status=="PARTIAL"`` to ``member_status="partial"``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMFanOut, OHMManifest, OHMMember, OHMMetadata, OHMRuntime
from oraclous_ohm.orchestrate import run_team

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, *, depends_on: list[str] | None = None) -> OHMMember:
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


async def test_partial_member_records_partial_and_downstream_runs() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "a":
            return {"output": "best-effort so far", "status": "PARTIAL"}  # a degraded
        return {"output": member.role, "status": "SUCCEEDED"}

    res = await run_team(_team([_m("a"), _m("b", depends_on=["a"])]), dispatch)
    assert res.member_status["a"] == "partial"  # degrade → partial, NOT succeeded
    assert res.member_status["b"] == "succeeded"  # downstream still ran (partial is not a failure)


async def test_partial_member_does_not_cascade_fail_the_team() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"output": "x", "status": "PARTIAL"}

    res = await run_team(_team([_m("a")]), dispatch)
    assert res.member_status["a"] == "partial"
    assert res.status == "completed"  # a degraded member does NOT make the run failed


async def test_fan_out_with_a_degraded_item_is_recorded_partial() -> None:
    # #587 (review SHOULD-FIX): a deterministic reduce STRIPS the per-item status, so a fan-out
    # member with a degraded sub-dispatch must STILL be recorded "partial", not "succeeded".
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        status = "PARTIAL" if item == "b" else "SUCCEEDED"  # one of three items degrades
        return {"output": f"out-{item}", "status": status}

    fan = OHMMember(
        role="w",
        kind="agent",
        manifest_ref="org:x/w@1",
        fan_out=OHMFanOut(over="$.items", max_parallel=2, reduce="concat"),
    )
    res = await run_team(_team([fan]), dispatch, state={"items": ["a", "b", "c"]})
    assert (
        res.member_status["w"] == "partial"
    )  # a degraded sub-run makes the fan-out member partial
    assert res.status == "completed"  # still not a failure


async def test_a_real_failure_still_outranks_a_partial() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "a":
            return {"output": "x", "status": "PARTIAL"}
        raise RuntimeError("b boom")  # an independent member genuinely fails

    res = await run_team(_team([_m("a"), _m("b")]), dispatch)
    assert res.member_status["a"] == "partial"  # the degrade is recorded
    assert res.status == "failed"  # but a real failure still outranks → re-runnable
