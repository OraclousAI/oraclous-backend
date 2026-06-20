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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
) -> TeamRunResult:
    """Execute a Team Harness member DAG stage by stage, a real fan-in barrier between stages."""
    state = state or {}
    by_role = {m.role: m for m in manifest.members}
    stages = manifest.execution_stages()  # topological_stages — fail-closed on cycle/unknown/dup
    results: dict[str, Any] = {}
    envelopes: list[HandoffEnvelope] = []
    skipped: list[str] = []

    async def run_member(role: str) -> None:
        member = by_role[role]
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
            items = _resolve_over(member.fan_out.over, state, results)
            results[role] = await _gather_capped(
                [dispatch(member, inbound, item) for item in items], member.fan_out.max_parallel
            )
        else:
            results[role] = await dispatch(member, inbound, None)

    for stage in stages:
        await asyncio.gather(*(run_member(role) for role in stage))  # the fan-in barrier

    return TeamRunResult(results=results, envelopes=envelopes, skipped=skipped, stages=stages)
