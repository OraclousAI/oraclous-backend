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
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.aggregate import aggregate_reduce
from oraclous_ohm.dag import topological_stages
from oraclous_ohm.envelope import HandoffEnvelope, build_handoff
from oraclous_ohm.errors import OHMError
from oraclous_ohm.manifest import OHMLoop, OHMManifest, OHMMember, OHMOrchestration, OHMRunIf
from oraclous_ohm.precedence_resolution import clamp_member_source

# Dispatch one member (+ optional fan-out item) given its inbound hand-offs -> output payload.
# LEAK-SAFETY CONTRACT (ADR-042 / CLAUDE.md §11): when dispatch RAISES, ``str(exc)`` is recorded in
# ``member_errors`` and persisted + served in the team-run ``error_message`` — so the raised error
# MUST be leak-safe (a coarse status/role, never an upstream response body, prompt, or model text).
# The engine's ``make_harness_dispatch`` honours this (it surfaces the harness's own error_type, not
# a provider body; the LLM client already strips the body — see openai_compatible.py's classifier).
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
    members: list[OHMMember] | None = None,
) -> TeamRunResult:
    """Execute a Team Harness member DAG stage by stage, a real fan-in barrier between stages.

    A ``kind: human`` member is a BLOCKING gate (ADR-035 §6): the run PAUSES at its stage until the
    gate is advanced via ``gate_decisions[role]`` ('approve' / 'reject'); downstream ``depends_on``
    members cannot run until it is approved — agents cannot cross a human gate by any path.

    ``completed`` seeds the results of members that ALREADY ran in a prior drive (resume past a
    human gate): those members are NOT dispatched again — their cached output is reused (so inbound
    hand-offs still thread downstream), which makes ``advance`` idempotent over a member's side
    effects instead of re-executing the whole DAG.

    ``members`` overrides the member set the DAG is built over — the hybrid driver (ADR-043 #552)
    passes the acyclic skeleton + one condensed node per loop, so ``run_team`` schedules the loops
    at their topological position. ``None`` ⇒ ``manifest.members`` (existing callers unchanged).
    """
    state = state or {}
    gates = gate_decisions or {}
    done = completed or {}
    _members = members if members is not None else manifest.members
    by_role = {m.role: m for m in _members}
    stages = topological_stages(_members)  # fail-closed on cycle/unknown/dup
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
        try:
            # Build the inbound hand-offs INSIDE the try (ADR-042): build_handoff fail-closes
            # (raises OHMHandoffError) when an upstream payload violates a typed output contract —
            # that is a per-member failure, NOT a reason to abort the team. Outside the try it would
            # escape run_member → cancel the gather siblings → re-raise → all-or-nothing team abort.
            inbound: list[HandoffEnvelope] = []
            for dep in member.depends_on:
                produced = results.get(dep)
                if produced is None:
                    continue
                payload = produced if isinstance(produced, dict) else {"output": produced}
                env = build_handoff(
                    by_role[dep], member, payload, objective_slice=member.subgoal or ""
                )
                if _floor_tier is not None:  # precedence declared → tag the (clamped) source tier
                    env = env.model_copy(update={"source_layer": _floor_tier})
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
            # The member's hand-off validation (build_handoff) OR its harness dispatch failed (a
            # hand-off-schema violation, a transient-exhausted FAILED, a budget escalation, or a
            # hard error). Record it FAILED + the leak-safe detail; do NOT re-raise — the stage's
            # other (independent) members + the next stage still run. The run is then "failed" (not
            # SUCCEEDED) and the failed+blocked members are re-runnable.
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
        # A human gate whose own upstream FAILED/BLOCKED is NOT a real decision point — exclude it
        # from the pause/reject check so a failed producer never surfaces as a healthy PAUSED run;
        # run_member then records it BLOCKED (and the run resolves to "failed").
        stage_gates = [
            r for r in stage if by_role[r].kind == "human" and not _blocked_by_upstream(r)
        ]
        # ADR-042 (#551): if a member already FAILED/BLOCKED in ANY branch, prefer the "failed"
        # verdict over PAUSING/REJECTING — a pending gate in a PARALLEL branch must not mask the
        # failure as a healthy PAUSED run (where rerun's FAILED-only guard would leave it
        # unrecoverable until the gate resolves). The run stops at the gate either way; reporting
        # "failed" makes the failed member re-runnable (the re-run re-drives it, then the run
        # reaches the gate and PAUSES normally). A gate with no recorded failure pauses as before.
        already_failed = any(s in ("failed", "blocked") for s in member_status.values())
        undecided = [g for g in stage_gates if gates.get(g) is None]
        if (
            undecided and not already_failed
        ):  # block: pause; downstream depends_on members do not run
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
        if rejected and not already_failed:  # the author rejected — halt; downstream does not run
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
        if (undecided or rejected) and already_failed:
            break  # a recorded failure outranks the gate → fall through to the "failed" verdict
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


# ── ADR-043 #552: the bounded conductor seam for ONE loop SCC ──────────────────────────────────
# A genuine loop (a Tarjan SCC isolated at import — see import_/assemble.py + the loops field)
# runs round-by-round through a bounded LLM-coordinator that ONLY PICKS the next member; every limit
# + the done-check is CODED. The two load-bearing invariants: (1) the team never satisfies its own
# done-check — a round converges only when the CODED ``done_check`` confirms (the coordinator's "I'm
# done" never finishes the run on its own); (2) four always-on runaway bounds (max rounds / wall /
# cost / no-progress), any one halting with a SAVED PARTIAL result — never an infinite loop, never a
# hard abort (#551 non-abort). ``dispatch``/``coordinate``/``done_check`` are INJECTED so the engine
# supplies the real BYOM coordinator + the real coverage-floor/landed-artifacts/evaluator grade.

# Coded loop done-check: given the loop's results so far, returns True iff DONE. INJECTED — the
# engine wires the real coverage-floor (produced-output + landed-artifacts) + separate-evaluator
# grade. The model NEVER decides done (ADR-043 invariant).
DoneCheckFn = Callable[[dict[str, Any]], Awaitable[bool]]
# The coordinator picks the next loop member(s) to run, given (loop, results-so-far, rounds-left);
# returns [] when it BELIEVES the goal is met (still confirmed by the coded done-check).
LoopCoordinateFn = Callable[["OHMLoop", dict[str, Any], int], Awaitable[list[str]]]

#: the terminal status of a loop seam — converged, or halted at one of the four runaway bounds.
LoopSeamStatus = Literal["converged", "max_rounds", "wall_time", "cost_budget", "no_progress"]


class LoopSeamResult(BaseModel):
    """The outcome of running ONE loop SCC through the bounded conductor seam (ADR-043 #552)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    results: dict[str, Any] = Field(default_factory=dict)  # role -> latest output (loop state)
    member_status: dict[str, str] = Field(default_factory=dict)  # role -> succeeded | failed
    member_errors: dict[str, str] = Field(default_factory=dict)  # role -> leak-safe failure detail
    envelopes: list[HandoffEnvelope] = Field(default_factory=list)
    rounds: int = 0
    status: LoopSeamStatus = "max_rounds"


def _loop_inbound(
    loop: OHMLoop,
    member: OHMMember,
    results: dict[str, Any],
    by_role: dict[str, OHMMember],
    envelopes: list[HandoffEnvelope],
) -> list[HandoffEnvelope]:
    """The hand-offs INTO ``member`` this round — every OTHER loop member's latest output, threaded
    with that member's ## Handoff next_task (the loop ``routing``) as the objective slice."""
    inbound: list[HandoffEnvelope] = []
    for other in loop.members:
        if other == member.role:
            continue
        produced = results.get(other)
        if produced is None or other not in by_role:
            continue
        payload = produced if isinstance(produced, dict) else {"output": produced}
        env = build_handoff(
            by_role[other],
            member,
            payload,
            objective_slice=loop.routing.get(other, "") or member.subgoal or "",
        )
        inbound.append(env)
        envelopes.append(env)
    return inbound


def _progress_signature(results: dict[str, Any], member_status: dict[str, str]) -> str:
    """A stable signature of the loop's observable state — used to detect a no-progress round (one
    where neither the results nor any member status changed)."""
    return json.dumps(
        {"r": results, "s": sorted(member_status.items())}, sort_keys=True, default=str
    )


async def run_loop_seam(
    loop: OHMLoop,
    by_role: dict[str, OHMMember],
    dispatch: DispatchFn,
    coordinate: LoopCoordinateFn,
    done_check: DoneCheckFn,
    *,
    max_rounds: int,
    max_wall_seconds: float | None = None,
    max_cost: int | None = None,
    cost_so_far: Callable[[], int] | None = None,
    clock: Callable[[], float] | None = None,
    seed_results: dict[str, Any] | None = None,
) -> LoopSeamResult:
    """Run ONE loop SCC round-by-round under coded governance (ADR-043 #552); see module note."""
    _clock = clock or time.monotonic
    _cost = cost_so_far or (lambda: 0)
    loop_roles = set(loop.members)
    results: dict[str, Any] = dict(seed_results or {})
    member_status: dict[str, str] = {}
    member_errors: dict[str, str] = {}
    envelopes: list[HandoffEnvelope] = []
    started = _clock()
    rounds = 0
    last_signature: str | None = None

    def _result(status: LoopSeamStatus) -> LoopSeamResult:
        return LoopSeamResult(
            results=results,
            member_status=member_status,
            member_errors=member_errors,
            envelopes=envelopes,
            rounds=rounds,
            status=status,
        )

    while rounds < max_rounds:
        # bounds are checked BEFORE dispatching the round → halt-with-partial, never abort
        if max_wall_seconds is not None and _clock() - started > max_wall_seconds:
            return _result("wall_time")
        if max_cost is not None and _cost() > max_cost:
            return _result("cost_budget")

        next_roles = await coordinate(loop, dict(results), max_rounds - rounds)
        for role in next_roles:  # fail-closed: route ONLY to declared loop members
            if role not in loop_roles:
                raise OHMError(f"loop coordinator routed to non-loop member {role!r}")
        if not next_roles:
            # the coordinator believes the goal is met — the CODED check decides, not the model.
            return _result("converged" if await done_check(results) else "no_progress")

        rounds += 1
        for role in next_roles:
            member = by_role[role]
            inbound = _loop_inbound(loop, member, results, by_role, envelopes)
            try:
                results[role] = await dispatch(member, inbound, None)
                member_status[role] = "succeeded"
            except Exception as exc:  # noqa: BLE001 — #551 non-abort: record, never raise out of loop
                results.setdefault(role, None)
                member_status[role] = "failed"
                member_errors[role] = str(exc)[:2000] or type(exc).__name__

        if await done_check(results):  # the coded done-check (coverage-floor + evaluator) confirms
            return _result("converged")
        signature = _progress_signature(results, member_status)  # no-progress: nothing changed
        if signature == last_signature:
            return _result("no_progress")
        last_signature = signature

    return _result("max_rounds")
