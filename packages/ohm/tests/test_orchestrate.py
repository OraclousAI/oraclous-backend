"""The team-DAG orchestrator core (#419; ADR-035 §2).

A pure executor with an injected mock dispatch — proves the orchestration logic (stage barrier,
parallel-within-stage, fan_out expansion, envelope threading, conditional skip) with no live runtime
or docker. The durable execution-engine wiring (real harness dispatch) is the follow-up.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMFanOut, OHMManifest, OHMMember, OHMMetadata, OHMRuntime
from oraclous_ohm.orchestrate import run_team

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(
    role: str, depends_on: list[str] | None = None, fan_out: OHMFanOut | None = None
) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=depends_on or [],
        fan_out=fan_out,
    )


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def test_sequential_pipeline_threads_envelopes() -> None:
    calls: list[tuple[str, list[str]]] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        calls.append((member.role, [e.from_role for e in envs]))
        return {"out": member.role}

    res = await run_team(_team([_m("a"), _m("b", ["a"]), _m("c", ["b"])]), dispatch)
    assert [c[0] for c in calls] == ["a", "b", "c"]  # depends_on order
    assert calls[1][1] == ["a"]  # b received a's hand-off
    assert calls[2][1] == ["b"]
    assert res.results["c"] == {"out": "c"}


async def test_parallel_stage_barrier_and_fan_in() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        await asyncio.sleep(0.005)
        return {"r": member.role}

    res = await run_team(
        _team([_m("a"), _m("b", ["a"]), _m("c", ["a"]), _m("d", ["b", "c"])]), dispatch
    )
    assert res.stages == [["a"], ["b", "c"], ["d"]]  # b,c share a stage; d waits on both (barrier)
    assert {e.from_role for e in res.envelopes if e.to_role == "d"} == {"b", "c"}  # fan-in


async def test_real_concurrency_within_a_stage() -> None:
    running = 0
    peak = 0

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return {"r": member.role}

    await run_team(_team([_m("a"), _m("b", ["a"]), _m("c", ["a"])]), dispatch)
    assert peak >= 2  # b and c genuinely overlapped — a real parallel stage, not serial turns


async def test_fan_out_expansion() -> None:
    seen: list[Any] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(item)
        return {"item": item}

    members = [_m("researcher", fan_out=OHMFanOut(over="$.modules", max_parallel=2))]
    res = await run_team(_team(members), dispatch, state={"modules": ["m1", "m2", "m3"]})
    assert sorted(seen) == ["m1", "m2", "m3"]  # one dispatch per item
    assert len(res.results["researcher"]) == 3  # outputs collected


async def test_conditional_skip() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"ok": member.role}

    def predicate(member: OHMMember, results: dict[str, Any]) -> bool:
        return member.role != "b"  # skip b

    res = await run_team(
        _team([_m("a"), _m("b", ["a"]), _m("c", ["a"])]), dispatch, predicate=predicate
    )
    assert "b" in res.skipped
    assert res.results["b"] is None
    assert res.results["c"] == {"ok": "c"}
