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
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.aggregate import aggregate_reduce
from oraclous_ohm.envelope import HandoffEnvelope, build_handoff
from oraclous_ohm.errors import OHMError
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMOrchestration, OHMRunIf
from oraclous_ohm.precedence_resolution import clamp_member_source

# Dispatch one member (+ optional fan-out item) given its inbound hand-offs -> output payload.
DispatchFn = Callable[[OHMMember, list[HandoffEnvelope], Any], Awaitable[Any]]

# Stage fan-out cap (#543): a wide imported team (e.g. 18 members collapsed into one flat stage)
# would otherwise fire every member's LLM call at once against ONE shared per-org BYOM key and
# self-throttle (429), failing a random member non-deterministically. Bound how many members
# dispatch concurrently per stage; env-overridable for keys with higher rate limits.
_STAGE_CONCURRENCY = max(1, int(os.environ.get("OHM_TEAM_STAGE_CONCURRENCY") or "4"))
# Whether to dispatch a member given the results so far (conditional skip); default = always.
PredicateFn = Callable[[OHMMember, dict[str, Any]], bool]


class TeamRunResult(BaseModel):
    """The outcome of a team DAG run: per-member outputs, threaded envelopes, skips, the stages."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    results: dict[str, Any] = Field(default_factory=dict)  # role -> output (a list when fan_out)
    envelopes: list[HandoffEnvelope] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    stages: list[list[str]] = Field(default_factory=list)
    # ADR-042 (#551): a producing team run is "completed" (→ SUCCEEDED) ONLY when EVERY member
    # delivered; if any member FAILED or was BLOCKED by an upstream failure it is "failed" (→
    # FAILED), with the failed/blocked members re-runnable (re-drive with the succeeded members
    # seeded via ``completed``). One member failing no longer aborts the team (see ``run_member``).
    status: Literal["completed", "paused", "rejected", "failed"] = "completed"
    paused_at: list[str] = Field(default_factory=list)  # the human gate role(s) the run blocks on
    # ADR-042 per-member terminal status — role -> "succeeded"|"failed"|"blocked"|"skipped". The
    # team verdict derives from it (SUCCEEDED iff none failed/blocked); re-run targets the failures.
    member_status: dict[str, str] = Field(default_factory=dict)
    # role -> the failure detail for a "failed" member (leak-safe str of the dispatch error)
    member_errors: dict[str, str] = Field(default_factory=dict)


def _resolve_over(over: str, state: dict[str, Any], results: dict[str, Any]) -> list[Any]:
    """Resolve a ``fan_out.over`` path — supported shape is ``$.<key>`` into state or results."""
    key = over[2:] if over.startswith("$.") else over
    value = state.get(key, results.get(key))
    return list(value) if isinstance(value, (list, tuple)) else []


def _eval_run_if(cond: OHMRunIf, results: dict[str, Any]) -> bool:
    """Evaluate a declarative conditional (OHMMember.run_if) against produced results — a safe,
    no-eval comparison, FAIL-CLOSED (False) on a missing source or any type error. Returns True =
    run the member, False = skip it."""
    src = results.get(cond.from_role)
    if src is None:  # the gated-on member didn't run / produced nothing -> do not run
        return False
    value = src.get(cond.field) if (cond.field is not None and isinstance(src, dict)) else src
    try:
        match cond.op:
            case "truthy":
                return bool(value)
            case "eq":
                return bool(value == cond.value)
            case "ne":
                return bool(value != cond.value)
            case "in":
                return value in cond.value
            case "gt":
                return bool(value > cond.value)
            case "lt":
                return bool(value < cond.value)
            case "gte":
                return bool(value >= cond.value)
            case "lte":
                return bool(value <= cond.value)
    except TypeError:  # incomparable types / non-container 'in' -> fail-closed
        return False
    return False


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
    # ADR-042 (#551): per-member terminal status (role -> succeeded|failed|blocked|skipped) + the
    # failure detail for failed members. Seeded members (``done``) are recorded succeeded as run.
    member_status: dict[str, str] = {}
    member_errors: dict[str, str] = {}
    # team-level termination (ADR-035): a wall-clock deadline for the whole DAG run. max_rounds /
    # convergence apply to the cyclic/B2 path, not this single-pass DAG; max_wall_seconds binds.
    _max_wall = (
        manifest.orchestration.termination.max_wall_seconds if manifest.orchestration else None
    )
    _deadline = time.monotonic() + _max_wall if _max_wall else None
    # #514 Hierarchy-of-Truth: when the manifest declares a precedence order, a member's hand-off is
    # fail-closed to the non-canonical FLOOR tier — a member can never inject a canonical
    # (rules/bible/toc) claim through a hand-off. No precedence declared → tagging is off (None).
    _floor_tier: str | None = (
        clamp_member_source(None, manifest.precedence.order)
        if manifest.precedence is not None and manifest.precedence.order
        else None
    )

    def _blocked_by_upstream(role: str) -> bool:
        """ADR-042 (#551): True when a member's upstream dependency FAILED or is itself BLOCKED — it
        can't honour its inbound contract. Deps are in EARLIER topological stages, so their terminal
        status is already recorded here; this propagates BLOCKED transitively down the DAG. Applies
        to a human GATE too (a gate with a failed upstream is unproducible input — BLOCK, never
        PAUSE the run on it)."""
        return any(member_status.get(d) in ("failed", "blocked") for d in by_role[role].depends_on)

    async def run_member(role: str) -> None:
        if role in done:  # already executed in a prior drive — reuse, do not dispatch again
            member_status[role] = "succeeded"  # a seeded member already delivered (resume/re-run)
            return
        member = by_role[role]
        # ADR-042 non-aborting failure: a member (agent OR human gate) blocked by an upstream
        # failure is recorded BLOCKED (never dispatched, not raised), so independent members run and
        # the failure is re-runnable. Checked FIRST so a blocked gate cannot reach the human branch
        # below and pause the run on unproducible input.
        if _blocked_by_upstream(role):
            results[role] = None
            member_status[role] = "blocked"
            return
        if member.kind == "human":
            # a blocking gate — never dispatched; by the time we run it, it is an approved decision
            results[role] = {"gate": role, "decision": gates.get(role)}
            member_status[role] = "succeeded"  # an approved gate delivered its decision
            return
        if predicate is not None and not predicate(member, results):
            skipped.append(role)
            results[role] = None
            member_status[role] = "skipped"
            return
        if member.run_if is not None and not _eval_run_if(member.run_if, results):
            # declarative conditional dispatch (ADR-035): a prior output did not satisfy the test
            skipped.append(role)
            results[role] = None
            member_status[role] = "skipped"
            return
        inbound: list[HandoffEnvelope] = []
        for dep in member.depends_on:
            produced = results.get(dep)
            if produced is None:
                continue
            payload = produced if isinstance(produced, dict) else {"output": produced}
            env = build_handoff(by_role[dep], member, payload, objective_slice=member.subgoal or "")
            if _floor_tier is not None:  # precedence declared → tag the hand-off's (clamped) tier
                env = env.model_copy(update={"source_layer": _floor_tier})
            inbound.append(env)
            envelopes.append(env)
        try:
            if member.fan_out is not None:
                fan = member.fan_out
                items = _resolve_over(fan.over, state, results)
                outputs = await _gather_capped(
                    [dispatch(member, inbound, item) for item in items], fan.max_parallel
                )
                if fan.reduce == "synthesize":
                    # ADR-035 B3: an LLM-SYNTHESIS pass merges the N outputs into one (not a
                    # deterministic concat; EURail: 14 batches -> 1 ledger) — the member is
                    # dispatched once more over all N outputs, through dispatch (the harness/LLM).
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
        except Exception as exc:  # noqa: BLE001 — ADR-042: record FAILED, never abort the team DAG
            # The member's harness dispatch failed (a transient-exhausted FAILED, a budget/iteration
            # escalation, or a hard error). Record it FAILED + the leak-safe detail; do NOT re-raise
            # — the stage's other (independent) members and the next stage still run. The whole run
            # is then "failed" (not SUCCEEDED) and the failed+blocked members are re-runnable.
            results[role] = None
            member_status[role] = "failed"
            member_errors[role] = str(exc)[:2000] or type(exc).__name__
            return
        member_status[role] = "succeeded"

    # Stage fan-out cap (#543): bound how many members dispatch concurrently so a wide stage cannot
    # self-throttle the shared BYOM key. Wraps the run_member calls (NOT the inner fan_out dispatch,
    # which keeps its own max_parallel) so there is no semaphore re-entrancy / deadlock.
    stage_sem = asyncio.Semaphore(_STAGE_CONCURRENCY)

    async def _bounded(role: str) -> None:
        async with stage_sem:
            await run_member(role)

    for stage in stages:
        # ADR-042 (#551): a human gate whose upstream FAILED/BLOCKED is NOT a real decision point —
        # exclude it from the pause/reject check so a failed producer no longer surfaces as a
        # healthy PAUSED run; run_member then records it BLOCKED (and the run resolves to "failed").
        stage_gates = [
            r for r in stage if by_role[r].kind == "human" and not _blocked_by_upstream(r)
        ]
        undecided = [g for g in stage_gates if gates.get(g) is None]
        if undecided:  # block: pause the run; downstream depends_on members do not run
            return TeamRunResult(
                results=results,
                envelopes=envelopes,
                skipped=skipped,
                stages=stages,
                status="paused",
                paused_at=undecided,
                member_status=member_status,
                member_errors=member_errors,
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
                member_status=member_status,
                member_errors=member_errors,
            )
        barrier = asyncio.gather(*(_bounded(role) for role in stage))  # the fan-in barrier (capped)
        if _deadline is not None:  # enforce the team wall-clock deadline across the barrier
            remaining = _deadline - time.monotonic()
            if remaining <= 0:
                barrier.cancel()
                raise OHMError(f"team run exceeded max_wall_seconds ({_max_wall})")
            try:
                await asyncio.wait_for(barrier, timeout=remaining)
            except TimeoutError as exc:
                raise OHMError(f"team run exceeded max_wall_seconds ({_max_wall})") from exc
        else:
            await barrier

    # ADR-042 verdict: SUCCEEDED ("completed") iff EVERY member delivered — any FAILED or BLOCKED
    # member makes the run "failed" (→ FAILED), re-runnable by re-driving the failed+blocked members
    # with the succeeded ones seeded via ``completed``. A skipped (conditional) member is a declared
    # no-op, not a failure, so it does not block SUCCEEDED.
    final_status = (
        "failed" if any(s in ("failed", "blocked") for s in member_status.values()) else "completed"
    )
    return TeamRunResult(
        results=results,
        envelopes=envelopes,
        skipped=skipped,
        stages=stages,
        status=final_status,
        member_status=member_status,
        member_errors=member_errors,
    )


# ── B2: the prose-interpreting orchestration agent (ADR-035; OPT-IN, behind a flag) ──────────
# Decide the next member role(s) to dispatch, given the orchestration brief, the results so far, and
# the not-yet-run members. Returns [] when the coordinator declares the goal met (success_criteria).
# INJECTED — an LLM coordinator in the runtime; a deterministic stand-in in tests.
CoordinateFn = Callable[[OHMOrchestration, dict[str, Any], list[str]], Awaitable[list[str]]]


async def run_team_coordinated(
    manifest: OHMManifest,
    dispatch: DispatchFn,
    coordinate: CoordinateFn,
    *,
    state: dict[str, Any] | None = None,
    max_rounds: int = 20,
) -> TeamRunResult:
    """B2 (OPT-IN): a PROSE-interpreting coordinator routes the team instead of the fixed DAG.

    "Choice is prose, mechanics are coded" (ADR-035): the coordinator (an LLM in the runtime) reads
    the ``orchestration`` brief + the results so far and picks the next member(s) to dispatch, until
    it declares the goal met (returns ``[]``). But the MECHANICS are coded and non-overridable —

    - the coordinator may route ONLY to DECLARED members (the R4 T3-M1 guardrail): a route to an
      unknown member is fail-closed (``OHMError``), so no prose path grants a member/capability the
      manifest never declared; and
    - the loop is bounded by a coded TERMINATION (``max_rounds`` ∩ ``orchestration.termination``),
      so a coordinator that never converges cannot run away.

    This ships behind a flag; the generated DAG (``run_team``) is the default path. ``dispatch`` is
    the same injected member-dispatch ``run_team`` uses (so the per-member ceiling still binds)."""
    state = state or {}
    brief = manifest.orchestration or OHMOrchestration()
    by_role = {m.role: m for m in manifest.members}
    results: dict[str, Any] = {}
    envelopes: list[HandoffEnvelope] = []
    cap = min(max_rounds, brief.termination.max_rounds or max_rounds)

    async def run_one(role: str) -> None:
        member = by_role[role]
        inbound: list[HandoffEnvelope] = []
        for dep in member.depends_on:
            produced = results.get(dep)
            if produced is None:
                continue
            payload = produced if isinstance(produced, dict) else {"output": produced}
            env = build_handoff(by_role[dep], member, payload, objective_slice=member.subgoal or "")
            inbound.append(env)
            envelopes.append(env)
        results[role] = await dispatch(member, inbound, None)

    rounds = 0
    while rounds < cap:
        rounds += 1
        remaining = [r for r in by_role if r not in results]
        next_roles = await coordinate(brief, dict(results), remaining)
        if not next_roles:  # the coordinator declared the goal met (prose success_criteria)
            break
        for role in next_roles:  # GUARDRAIL: route only to declared members (fail-closed)
            if role not in by_role:
                raise OHMError(f"coordinator routed to undeclared member {role!r}")
        await asyncio.gather(*(run_one(role) for role in next_roles))  # a coordinator-chosen stage

    return TeamRunResult(results=results, envelopes=envelopes, stages=[], status="completed")
