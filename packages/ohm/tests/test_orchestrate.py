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
from oraclous_ohm.errors import OHMError
from oraclous_ohm.manifest import (
    OHMFanOut,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRunIf,
    OHMRuntime,
    OHMTermination,
)
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


def _team(members: list[OHMMember], orchestration: OHMOrchestration | None = None) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        orchestration=orchestration,
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
    assert len(res.results["researcher"]) == 3  # outputs collected (default concat)


async def test_fan_out_outputs_are_merged_by_the_reducer() -> None:
    # ADR-035 B3: fan-out outputs MERGE through the reducer (EURail: N batches -> 1 ledger),
    # replacing the round-table's last-writer-wins.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"evidence": [item]}  # each instance contributes an evidence list

    fan = OHMFanOut(over="$.items", max_parallel=2, reduce="concat", reduce_field="evidence")
    res = await run_team(_team([_m("r", fan_out=fan)]), dispatch, state={"items": ["a", "b", "c"]})
    assert res.results["r"] == ["a", "b", "c"]  # 3 evidence lists merged into one, not 3 raw dicts


async def test_fan_out_synthesize_merges_via_an_llm_pass() -> None:
    # ADR-035 B3: reduce="synthesize" dispatches the member ONCE MORE over the N outputs (an LLM
    # synthesis — EURail's ledger), so the result is the synthesized merge, not a concat.
    calls: list[Any] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        calls.append(item)
        if isinstance(item, dict) and "synthesize" in item:
            return {"ledger": f"merged {len(item['synthesize'])} batches"}
        return {"batch": item}

    fan = OHMFanOut(
        over="$.items", max_parallel=2, reduce="synthesize", synthesize_prompt="merge into a ledger"
    )
    res = await run_team(_team([_m("r", fan_out=fan)]), dispatch, state={"items": ["a", "b", "c"]})
    assert res.results["r"] == {"ledger": "merged 3 batches"}  # synthesized, not a raw list
    assert len(calls) == 4  # 3 fan-out instances + 1 synthesis pass over their outputs
    assert (
        calls[-1]["instruction"] == "merge into a ledger"
    )  # the prompt threaded into the synthesis


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


# ── (A) declarative conditional dispatch (run_if) — reachable via the manifest ───────────────


def _cond(role: str, deps: list[str], run_if: OHMRunIf) -> OHMMember:
    return OHMMember(
        role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=deps, run_if=run_if
    )


async def test_run_if_skips_the_member_when_a_prior_output_fails_the_test() -> None:
    # bitcoin: dispatch instrument-design ONLY if research regime is tradeable. Here flat -> skip.
    seen: list[str] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(member.role)
        return {"regime": "flat"} if member.role == "research" else {"ok": member.role}

    instrument = _cond(
        "instrument",
        ["research"],
        OHMRunIf(from_role="research", field="regime", op="eq", value="tradeable"),
    )
    res = await run_team(_team([_m("research"), instrument]), dispatch)
    assert "instrument" in res.skipped  # the regime was flat -> conditionally skipped
    assert "instrument" not in seen  # never dispatched
    assert res.results["instrument"] is None


async def test_run_if_runs_the_member_when_a_prior_output_satisfies_the_test() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"regime": "tradeable"} if member.role == "research" else {"ok": member.role}

    instrument = _cond(
        "instrument",
        ["research"],
        OHMRunIf(from_role="research", field="regime", op="eq", value="tradeable"),
    )
    res = await run_team(_team([_m("research"), instrument]), dispatch)
    assert "instrument" not in res.skipped
    assert res.results["instrument"] == {"ok": "instrument"}


# ── (B) team-level termination: max_wall_seconds bounds the DAG run ──────────────────────────


async def test_max_wall_seconds_fails_a_runaway_team() -> None:
    # ADR-035 termination: a DAG run that exceeds the team wall-clock fails (vs running unbounded).
    async def slow(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        await asyncio.sleep(2.0)
        return {"r": member.role}

    brief = OHMOrchestration(termination=OHMTermination(max_wall_seconds=1))
    import pytest

    with pytest.raises(OHMError, match="max_wall_seconds"):
        await run_team(_team([_m("a")], orchestration=brief), slow)


async def test_no_termination_means_no_deadline() -> None:
    # the default (no max_wall_seconds) imposes no deadline — a normal run completes unchanged.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"r": member.role}

    res = await run_team(_team([_m("a")]), dispatch)  # no orchestration block
    assert res.status == "completed" and res.results["a"] == {"r": "a"}
