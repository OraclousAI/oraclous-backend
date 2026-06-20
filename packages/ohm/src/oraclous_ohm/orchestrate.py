"""The team-DAG orchestrator core — the three orchestrator patterns + a real fan-in barrier (#419).

ADR-035 §2. A pure, dispatch-injected executor of a Team Harness's member DAG: members in one stage
run concurrently (``asyncio.gather``) and the next stage waits on a REAL fan-in barrier until the
prior stage completes — replacing the round-table's serial ``actor = actors[turn % n]`` loop and its
``transcript[-1]`` last-writer merge. It expands ``fan_out`` (one instance per item, capped at
``max_parallel``), threads the typed ``HandoffEnvelope`` member→member along ``depends_on``, and
supports conditional skip. ``dispatch`` is INJECTED — the durable sub-run dispatch (job_service /
harness) is wired by the execution-engine; here it is abstract, so the orchestration logic is
unit-testable without a runtime. ``sequential`` (a linear DAG = stages of one), ``parallel`` (a wide
stage + ``fan_out``), and ``conditional`` (a skip predicate) are the three patterns this realizes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.aggregate import aggregate_reduce
from oraclous_ohm.envelope import HandoffEnvelope, build_handoff
from oraclous_ohm.manifest import OHMManifest, OHMMember

# Dispatch one member (+ optional fan-out item) given its inbound hand-offs -> output payload.
DispatchFn = Callable[[OHMMember, list[HandoffEnvelope], Any], Awaitable[Any]]
# Whether to dispatch a member given the results so far (conditional skip); default = always.
PredicateFn = Callable[[OHMMember, dict[str, Any]], bool]


class TeamRunResult(BaseModel):
    """The outcome of a team DAG run: per-member outputs, threaded envelopes, skips, the stages."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    results: dict[str, Any] = Field(default_factory=dict)  # role -> output (a list when fan_out)
    envelopes: list[HandoffEnvelope] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    stages: list[list[str]] = Field(default_factory=list)
    status: Literal["completed", "paused", "rejected"] = "completed"
    paused_at: list[str] = Field(default_factory=list)  # the human gate role(s) the run blocks on


def _resolve_over(over: str, state: dict[str, Any], results: dict[str, Any]) -> list[Any]:
    """Resolve a ``fan_out.over`` path — supported shape is ``$.<key>`` into state or results."""
    key = over[2:] if over.startswith("$.") else over
    value = state.get(key, results.get(key))
    return list(value) if isinstance(value, (list, tuple)) else []


async def _gather_capped(coros: list[Awaitable[Any]], max_parallel: int) -> list[Any]:
    """Await all coroutines, at most ``max_parallel`` concurrently (the fan_out cap)."""
    sem = asyncio.Semaphore(max(1, max_parallel))

    async def bounded(coro: Awaitable[Any]) -> Any:
        async with sem:
            return await coro

    return list(await asyncio.gather(*(bounded(c) for c in coros)))


async def run_team(
    manifest: OHMManifest,
    dispatch: DispatchFn,
    *,
    state: dict[str, Any] | None = None,
    predicate: PredicateFn | None = None,
    gate_decisions: dict[str, str] | None = None,
    completed: dict[str, Any] | None = None,
) -> TeamRunResult:
    """Execute a Team Harness member DAG stage by stage, a real fan-in barrier between stages.

    A ``kind: human`` member is a BLOCKING gate (ADR-035 §6): the run PAUSES at its stage until the
    gate is advanced via ``gate_decisions[role]`` ('approve' / 'reject'); downstream ``depends_on``
    members cannot run until it is approved — agents cannot cross a human gate by any path.

    ``completed`` seeds the results of members that ALREADY ran in a prior drive (resume past a
    human gate): those members are NOT dispatched again — their cached output is reused (so inbound
    hand-offs still thread downstream), which makes ``advance`` idempotent over a member's side
    effects instead of re-executing the whole DAG.
    """
    state = state or {}
    gates = gate_decisions or {}
    done = completed or {}
    by_role = {m.role: m for m in manifest.members}
    stages = manifest.execution_stages()  # topological_stages — fail-closed on cycle/unknown/dup
    results: dict[str, Any] = dict(done)  # reuse already-completed members (resume), never re-run
    envelopes: list[HandoffEnvelope] = []
    skipped: list[str] = []

    async def run_member(role: str) -> None:
        if role in done:  # already executed in a prior drive — reuse, do not dispatch again
            return
        member = by_role[role]
        if member.kind == "human":
            # a blocking gate — never dispatched; by the time we run it, it is an approved decision
            results[role] = {"gate": role, "decision": gates.get(role)}
            return
        if predicate is not None and not predicate(member, results):
            skipped.append(role)
            results[role] = None
            return
        inbound: list[HandoffEnvelope] = []
        for dep in member.depends_on:
            produced = results.get(dep)
            if produced is None:
                continue
            payload = produced if isinstance(produced, dict) else {"output": produced}
            env = build_handoff(by_role[dep], member, payload, objective_slice=member.subgoal or "")
            inbound.append(env)
            envelopes.append(env)
        if member.fan_out is not None:
            fan = member.fan_out
            items = _resolve_over(fan.over, state, results)
            outputs = await _gather_capped(
                [dispatch(member, inbound, item) for item in items], fan.max_parallel
            )
            if fan.reduce == "synthesize":
                # ADR-035 B3: an LLM-SYNTHESIS pass merges the N outputs into one (not a
                # deterministic concat; EURail: 14 batches -> 1 ledger) — the member is dispatched
                # once more over all N outputs, through dispatch (the harness/LLM).
                syn_item = {"synthesize": outputs, "instruction": fan.synthesize_prompt or ""}
                results[role] = await dispatch(member, inbound, syn_item)
            else:
                # MERGE the fan-out outputs via the DETERMINISTIC reducer (ADR-035 B3).
                results[role] = aggregate_reduce(
                    outputs,
                    strategy=fan.reduce,
                    field=fan.reduce_field,
                    on=fan.reduce_key,
                    key=fan.reduce_key,
                )
        else:
            results[role] = await dispatch(member, inbound, None)

    for stage in stages:
        stage_gates = [r for r in stage if by_role[r].kind == "human"]
        undecided = [g for g in stage_gates if gates.get(g) is None]
        if undecided:  # block: pause the run; downstream depends_on members do not run
            return TeamRunResult(
                results=results,
                envelopes=envelopes,
                skipped=skipped,
                stages=stages,
                status="paused",
                paused_at=undecided,
            )
        rejected = [g for g in stage_gates if gates.get(g) == "reject"]
        if rejected:  # the author rejected — halt; downstream does not run
            return TeamRunResult(
                results=results,
                envelopes=envelopes,
                skipped=skipped,
                stages=stages,
                status="rejected",
                paused_at=rejected,
            )
        await asyncio.gather(*(run_member(role) for role in stage))  # the fan-in barrier

    return TeamRunResult(
        results=results, envelopes=envelopes, skipped=skipped, stages=stages, status="completed"
    )
