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
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.aggregate import aggregate_reduce
from oraclous_ohm.dag import topological_stages
from oraclous_ohm.envelope import HandoffEnvelope, build_handoff
from oraclous_ohm.errors import OHMError
from oraclous_ohm.gate import gate_verb
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
    # #585 (ADR-031 §D3): "cost_budget" is the GOVERNED budget-halt terminal (the team-pooled
    # ceiling was hit before all members/items dispatched) — distinct from "failed" (a member
    # error, ADR-042). It maps engine-side to a halted/COST_BUDGET terminal, NOT FAILED.
    status: Literal["completed", "paused", "rejected", "failed", "cost_budget"] = "completed"
    paused_at: list[str] = Field(default_factory=list)  # the human gate role(s) the run blocks on
    # ADR-042 per-member terminal status — role -> "succeeded"|"failed"|"blocked"|"skipped"|
    # "budget_skipped"|"partial". The team verdict derives from it (SUCCEEDED iff none failed/
    # blocked); re-run targets the failures. "budget_skipped" (#585) = un-attempted-by-budget;
    # "partial" (#587) = a degraded member (its loop exhausted under on_exhaustion=degrade) — both
    # NOT a member fault, so neither makes the team "failed".
    member_status: dict[str, str] = Field(default_factory=dict)
    # role -> the failure detail for a "failed" member (leak-safe str of the dispatch error)
    member_errors: dict[str, str] = Field(default_factory=dict)
    # PR-C (ADR-043 #552): per-loop checkpoint — "<loop_index>" -> {round, started_at, status}. The
    # hybrid driver sets it so the engine can persist it + resume a loop at a round boundary.
    loop_state: dict[str, Any] = Field(default_factory=dict)
    # #585: True iff the run halted on the team-pooled budget ceiling before every member/fan-out
    # item ran — the run completed PARTIALLY, fail-closed, not in error.
    partial: bool = False


def _resolve_over(over: str, state: dict[str, Any], results: dict[str, Any]) -> list[Any]:
    """Resolve a ``fan_out.over`` path — ``$.<key>`` into a user-seeded ``state`` value OR an
    upstream producer's output (#599). ``state`` wins over a same-named producer (checked first). A
    user input is a BARE list. A producer's dispatch result is WRAPPED (``{"output": <out>, ...}``);
    its ``output`` is the member's REAL harness output — a STRING for an LLM member (an outliner
    emitting ``["Ch1","Ch2"]``), so a string is parsed for an embedded JSON array. A non-list /
    non-JSON value yields ``[]`` (fail-soft — a misconfigured ``over`` never errors, no items)."""
    key = over[2:] if over.startswith("$.") else over
    if key in state:
        value: Any = state[key]  # user-seeded input (a bare list)
    else:
        produced = results.get(key)  # an upstream producer's wrapped dispatch result
        # #599: unwrap the producer's `output` so a list output drives a downstream fan_out.over
        value = (
            produced["output"] if isinstance(produced, dict) and "output" in produced else produced
        )
    if isinstance(value, str):
        # #599: a producer member's real harness output is TEXT — parse an embedded JSON array out
        # of it (the model is instructed to emit one), so a REAL member that emits a list (the
        # outliner → chapter-writers case) drives a downstream fan_out.over without a fake.
        match = re.search(r"\[.*\]", value, re.DOTALL)
        if match is not None:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, list):
                    value = parsed
            except ValueError:
                pass
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


class _Pool:
    """The per-run shared TEAM-POOLED budget tally (#585 / ADR-031 §D3). The token axis reads the
    engine's live ``cost_so_far`` (Σ on_cost); the sub-run axis is counted at admission; USD binds
    only when the harness surfaces it (token + sub_runs otherwise). Fail-closed: an ambiguous tally
    counts as spent, never headroom."""

    def __init__(
        self,
        *,
        max_tokens: int | None,
        max_sub_runs: int | None,
        max_usd: float | None,
        cost_so_far: Callable[[], int] | None,
    ) -> None:
        self._max_tokens = max_tokens
        self._max_sub_runs = max_sub_runs
        self._max_usd = max_usd
        self._cost_so_far = cost_so_far
        self.sub_runs = 0
        self.usd = 0.0

    def active(self) -> bool:
        """Any pooled ceiling resolved? None on every axis → the #576 path is unchanged."""
        return (
            self._max_tokens is not None
            or self._max_sub_runs is not None
            or self._max_usd is not None
        )

    def remaining_sub_runs(self) -> int | None:
        """Sub-run headroom (None = unbounded). Clamps a fan-out batch so the COUNT axis is EXACT —
        the count is known before dispatch (unlike tokens), so it must never overshoot."""
        return None if self._max_sub_runs is None else max(0, self._max_sub_runs - self.sub_runs)

    def would_exceed(self) -> bool:
        """True iff admitting one MORE dispatch would cross a resolved ceiling (FAIL-CLOSED)."""
        if self._max_sub_runs is not None and self.sub_runs >= self._max_sub_runs:
            return True
        if self._max_tokens is not None:
            # fail-closed: an unmeasurable token ceiling (no tally wired) is SPENT, not headroom —
            # never silently leave max_tokens_total unenforced (CLAUDE.md §3.5).
            if self._cost_so_far is None or self._cost_so_far() >= self._max_tokens:
                return True
        if self._max_usd is not None and self.usd >= self._max_usd:
            return True
        return False


async def _admit_fan_out(
    items: list[Any],
    run_item: Callable[[Any], Awaitable[Any]],
    max_parallel: int,
    pool: _Pool | None,
) -> tuple[list[Any], bool]:
    """Dispatch fan-out items in admission ORDER, checking the pooled ceiling BEFORE each batch of
    up to ``max_parallel`` — so the running tally (updated as a batch completes) gates the NEXT
    batch (a pure all-at-once gather reads ``cost_so_far()==0`` for every item). Stops admitting
    once the pool is exhausted; the un-admitted items never run. Returns (outputs, budget_hit). With
    no active pool it is the plain capped gather — the #576 path byte-for-byte unchanged."""
    if pool is None or not pool.active():
        return await _gather_capped([run_item(it) for it in items], max_parallel), False
    cap = max(1, max_parallel)
    outputs: list[Any] = []
    i = 0
    while i < len(items):
        if pool.would_exceed():
            return outputs, True
        # clamp the batch to the sub-run headroom so the COUNT axis never overshoots (the count is
        # known before dispatch); the token axis still soft-overshoots within a batch by design.
        rem = pool.remaining_sub_runs()
        batch_cap = cap if rem is None else min(cap, rem)
        batch = items[i : i + batch_cap]
        pool.sub_runs += len(batch)  # counted at admission — the sub-run axis is exact
        outputs.extend(await _gather_capped([run_item(it) for it in batch], cap))
        i += len(batch)
    return outputs, False


async def run_team(
    manifest: OHMManifest,
    dispatch: DispatchFn,
    *,
    state: dict[str, Any] | None = None,
    predicate: PredicateFn | None = None,
    gate_decisions: dict[str, Any] | None = None,
    completed: dict[str, Any] | None = None,
    members: list[OHMMember] | None = None,
    cost_so_far: Callable[[], int] | None = None,
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
    # #585 (ADR-031 §D3): the team-pooled budget gate, built ONCE + shared across every member +
    # fan-out item (the tally is team-wide). ``budget_exhausted`` halts the run fail-closed. With no
    # pooled ceiling resolved (no budget / all max_*_total None) the pool is inert — #576 unchanged.
    _budget = manifest.budget
    pool = _Pool(
        max_tokens=_budget.max_tokens_total if _budget else None,
        max_sub_runs=_budget.max_sub_runs if _budget else None,
        max_usd=_budget.max_usd_total if _budget else None,
        cost_so_far=cost_so_far,
    )
    budget_exhausted = False

    def _blocked_by_upstream(role: str) -> bool:
        """ADR-042 (#551): True when a member's upstream dependency FAILED or is itself BLOCKED — it
        can't honour its inbound contract. Deps are in EARLIER topological stages, so their terminal
        status is already recorded here; this propagates BLOCKED transitively down the DAG. Applies
        to a human GATE too (a gate with a failed upstream is unproducible input — BLOCK, never
        PAUSE the run on it)."""
        return any(member_status.get(d) in ("failed", "blocked") for d in by_role[role].depends_on)

    async def run_member(role: str) -> None:
        nonlocal budget_exhausted
        if role in done:  # already executed in a prior drive — reuse, do not dispatch again
            member_status[role] = "succeeded"  # a seeded member already delivered (resume/re-run)
            return
        if (
            budget_exhausted
        ):  # #585: a prior dispatch hit the pooled ceiling — halt, dispatch no more
            member_status[role] = "budget_skipped"
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
            # a blocking gate — never dispatched; by the time we run it, it is an APPROVED decision
            # (an undecided or `revise` gate re-pauses in the stage loop below, before this branch).
            # Record the normalized verb (a v1 bare string OR an ADR-046 GateDecision-shaped value).
            results[role] = {"gate": role, "decision": gate_verb(gates.get(role))}
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
                    by_role[dep],
                    member,
                    payload,
                    # #577: the producer's ## Handoff Next-task scopes the consumer's objective for
                    # this edge (mirrors the loop routing); else the consumer's own subgoal.
                    objective_slice=by_role[dep].handoff_objective or member.subgoal or "",
                )
                if _floor_tier is not None:  # precedence declared → tag the (clamped) source tier
                    env = env.model_copy(update={"source_layer": _floor_tier})
                inbound.append(env)
                envelopes.append(env)
            if member.fan_out is not None:
                fan = member.fan_out
                items = _resolve_over(fan.over, state, results)
                # #585: sequential-admission — the pooled ceiling gates each batch, so a runaway
                # `over` halts after the pool is spent, not after spawning every sub-run.
                outputs, fan_hit = await _admit_fan_out(
                    items, lambda it: dispatch(member, inbound, it), fan.max_parallel, pool
                )
                if (
                    fan_hit
                ):  # halted mid-fan-out — surface the raw partial, skip the extra reduce pass
                    budget_exhausted = True
                    member_status[role] = "budget_skipped"
                    results[role] = outputs
                    return
                if fan.reduce == "synthesize":
                    # ADR-035 B3: an LLM-SYNTHESIS pass merges the N outputs into one (not a
                    # deterministic concat; EURail: 14 batches -> 1 ledger) — the member is
                    # dispatched once more over all N outputs, through dispatch (the harness/LLM).
                    # #585: the synthesize is ANOTHER dispatch — gate it; if the pool is now spent,
                    # surface the raw partial rather than burn more (what the ceiling protects).
                    if pool.would_exceed():
                        budget_exhausted = True
                        member_status[role] = "budget_skipped"
                        results[role] = outputs
                        return
                    pool.sub_runs += 1
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
                # #587: a deterministic reduce STRIPS the per-item status, so the single-dispatch
                # check below can't see a degraded sub-run — a fan-out member is "partial" if ANY
                # sub-dispatch (a fan-out item OR the synthesize pass) degraded.
                reduced = results[role]
                degraded = (
                    isinstance(reduced, dict) and reduced.get("status") == "PARTIAL"
                ) or any(isinstance(o, dict) and o.get("status") == "PARTIAL" for o in outputs)
                member_status[role] = "partial" if degraded else "succeeded"
                return
            else:
                # #585: the pre-dispatch pooled ceiling gate for a single (non-fan-out) member —
                # multi-member team halts before the member that would cross the team total.
                if pool.would_exceed():
                    budget_exhausted = True
                    member_status[role] = "budget_skipped"
                    return
                pool.sub_runs += 1
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
        # #587: a DEGRADED member (its loop exhausted a budget under on_exhaustion=degrade) returns
        # a PARTIAL dispatch payload — record it "partial" (a 6th terminal), NOT "succeeded". It's a
        # governed graceful exhaustion: downstream still runs, and it is NOT a failure (has_failure
        # below excludes it), so the team verdict is not made "failed" by a degrade.
        out = results.get(role)
        member_status[role] = (
            "partial" if isinstance(out, dict) and out.get("status") == "PARTIAL" else "succeeded"
        )

    # Stage fan-out cap (#543): bound how many members dispatch concurrently so a wide stage cannot
    # self-throttle the shared BYOM key. Wraps the run_member calls (NOT the inner fan_out dispatch,
    # which keeps its own max_parallel) so there is no semaphore re-entrancy / deadlock.
    stage_sem = asyncio.Semaphore(_STAGE_CONCURRENCY)

    async def _bounded(role: str) -> None:
        async with stage_sem:
            await run_member(role)

    for stage in stages:
        if (
            budget_exhausted
        ):  # #585: a prior stage hit the pooled ceiling — halt (budget wins gates)
            break
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
        # A gate PAUSES the run when it is undecided (no decision yet) OR REVISE (ADR-046 §2, #578):
        # a `revise` re-pauses at the SAME gate after its invalidated producer sub-tree re-runs (the
        # service re-seeds ``completed = results − invalidation_set`` + threads the human's feedback
        # into those producers), so the human sees the fresh output and approves or revises again.
        # An ``approve`` falls through to run_member's human branch (crosses the gate); ``reject``
        # terminates below. ``gate_verb`` normalizes a bare string OR a GateDecision-shaped dict.
        pause_at = [g for g in stage_gates if gate_verb(gates.get(g)) in (None, "revise")]
        if (
            pause_at and not already_failed
        ):  # block: pause; downstream depends_on members do not run
            return TeamRunResult(
                results=results,
                envelopes=envelopes,
                skipped=skipped,
                stages=stages,
                status="paused",
                paused_at=pause_at,
                member_status=member_status,
                member_errors=member_errors,
            )
        rejected = [g for g in stage_gates if gate_verb(gates.get(g)) == "reject"]
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
        if (pause_at or rejected) and already_failed:
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
    if budget_exhausted:
        # #585 (folded with #587): the stage loop BREAKS at a budget halt, so later-stage members
        # were never visited — backfill them "budget_skipped" so member_status carries EVERY member
        # (ADR-042 contract + re-run targeting), not just the breaking stage. setdefault keeps any
        # already recorded (succeeded / partial / failed).
        for stage in stages:
            for role in stage:
                member_status.setdefault(role, "budget_skipped")
    has_failure = any(s in ("failed", "blocked") for s in member_status.values())
    # #585: a real member FAILURE outranks the budget halt (mirrors the gate-vs-failure precedence
    # above) — else a failed member would be masked as the healthy/non-re-runnable COST_BUDGET and
    # stranded (re-run needs FAILED). A budget halt on an otherwise-clean run is the partial.
    if budget_exhausted and not has_failure:
        return TeamRunResult(
            results=results,
            envelopes=envelopes,
            skipped=skipped,
            stages=stages,
            status="cost_budget",
            member_status=member_status,
            member_errors=member_errors,
            partial=True,
        )
    final_status = "failed" if has_failure else "completed"
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
            # #577: the producer's ## Handoff Next-task scopes the consumer's objective for the edge
            env = build_handoff(
                by_role[dep],
                member,
                payload,
                objective_slice=by_role[dep].handoff_objective or member.subgoal or "",
            )
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

#: the terminal status of a loop seam — converged, halted at a runaway bound, PAUSED on a per-round
#: HITL gate (PR-C), or ESCALATED after recalibration gave up (#553, slice 2/3).
LoopSeamStatus = Literal[
    "converged", "max_rounds", "wall_time", "cost_budget", "no_progress", "paused", "escalate"
]

#: the closed action set a recalibration may emit (#553) — no open-ended "think more".
RecalAction = Literal[
    "re-plan", "re-frame-objective", "change-strategy", "re-scope-member", "escalate"
]


class Diagnostic(BaseModel):
    """The CODED, external read of WHY a loop stalled (#553) — never the model's self-grade. Built
    from the loop's own observable state + the done-check's side-channel; handed to the recalibrator
    so the model only PICKS a tactic from the closed set, it does not diagnose itself."""

    model_config = ConfigDict(extra="ignore")

    stall_kind: Literal["coordinator", "signature"]  # which no_progress path fired
    missing_members: list[str] = Field(default_factory=list)  # coverage-floor gaps (produced None)
    failed_members: dict[str, str] = Field(default_factory=dict)  # role -> leak-safe error
    artifacts_landed: bool | None = None  # the done-check's landed-artifacts gate (None if unknown)
    evaluator_score: float | None = None  # the separate-evaluator scalar grade (None if no judge)
    evaluator_floor: float | None = None


class RecalDirective(BaseModel):
    """ONE recalibration directive (#553) — a closed-set action + the failed/blocked members to
    resume over. The ``reason`` is a leak-safe code token, never a raw member output."""

    model_config = ConfigDict(extra="ignore")

    action: RecalAction
    reason: str = ""
    member_targets: list[str] = Field(default_factory=list)


#: Given (loop, coded Diagnostic) return ONE directive, or None to halt fail-closed (#553).
RecalibrateFn = Callable[["OHMLoop", Diagnostic], Awaitable["RecalDirective | None"]]


class LoopSeamResult(BaseModel):
    """The outcome of running ONE loop SCC through the bounded conductor seam (ADR-043 #552)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    results: dict[str, Any] = Field(default_factory=dict)  # role -> latest output (loop state)
    member_status: dict[str, str] = Field(default_factory=dict)  # role -> succeeded | failed
    member_errors: dict[str, str] = Field(default_factory=dict)  # role -> leak-safe failure detail
    envelopes: list[HandoffEnvelope] = Field(default_factory=list)
    rounds: int = 0
    status: LoopSeamStatus = "max_rounds"
    # PR-C: the undecided per-round HITL gate role(s) the loop is PAUSED on (status == "paused")
    paused_at: list[str] = Field(default_factory=list)
    # #553: how many recalibrations this loop spent + the last directive (resume cursor + audit)
    recalibrations_used: int = 0
    last_directive: RecalDirective | None = None


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
    gate_decisions: dict[str, str] | None = None,
    resume_from_round: int = 0,
    started_at: float | None = None,
    recalibrate: RecalibrateFn | None = None,
    recalibration_cap: int = 1,
    done_check_diag: dict[str, Any] | None = None,
    resume_recalibrations_used: int = 0,
    resume_last_directive_digest: str | None = None,
) -> LoopSeamResult:
    """Run ONE loop SCC round-by-round under coded governance (ADR-043 #552); see module note.

    PR-C (step 6): a ``kind:"human"`` loop member is a per-round GATE — the loop PAUSES before a
    round while any gate is undecided (no auto-skip), resuming once ``gate_decisions`` carries the
    decision. ``resume_from_round`` + ``started_at`` make the seam resumable at a round boundary:
    the round counter continues (a resume can't buy a fresh round budget) and wall-clock is measured
    from the ORIGINAL run start (a long human pause does not reset the timeout). The four runaway
    bounds are checked FIRST each round, so a blown budget halts even with a gate pending."""
    _clock = clock or time.monotonic
    _cost = cost_so_far or (lambda: 0)
    gates = gate_decisions or {}
    loop_roles = set(loop.members)
    # PR-C: a kind:human loop member is a per-round approval gate (reuses run_team's human-gate
    # vocabulary). It is never harness-dispatched — rendered (its decision recorded) once decided.
    gate_roles = [r for r in loop.members if r in by_role and by_role[r].kind == "human"]
    results: dict[str, Any] = dict(seed_results or {})
    # ADR-042 (#551): a seeded loop member already delivered in a prior drive — record it succeeded
    # (mirrors run_team's ``completed`` handling) so an immediate convergence on resume (coordinator
    # returns [] because every member already produced) does not drop its terminal status.
    member_status: dict[str, str] = {
        role: "succeeded" for role in loop_roles if results.get(role) is not None
    }
    member_errors: dict[str, str] = {}
    envelopes: list[HandoffEnvelope] = []
    # PR-C: wall-clock from the ORIGINAL run start (persisted across a pause), never reset on resume
    started = started_at if started_at is not None else _clock()
    rounds = resume_from_round  # PR-C: resume continues the round counter, no fresh round budget
    last_signature: str | None = None
    # #553: bounded recalibration — clamp the cap to [0,3] (ADR-043: 1-2, max 3); resume the count +
    # the last directive digest so the cap + anti-repeat survive a HITL pause/approve cycle.
    cap = min(max(recalibration_cap, 0), 3)
    recalibrations_used = resume_recalibrations_used
    last_directive: RecalDirective | None = None
    last_directive_digest = resume_last_directive_digest

    def _result(status: LoopSeamStatus, paused_at: list[str] | None = None) -> LoopSeamResult:
        return LoopSeamResult(
            results=results,
            member_status=member_status,
            member_errors=member_errors,
            envelopes=envelopes,
            rounds=rounds,
            status=status,
            paused_at=paused_at or [],
            recalibrations_used=recalibrations_used,
            last_directive=last_directive,
        )

    def _diagnose(stall_kind: str) -> Diagnostic:
        # #553: the CODED, external read of the stall — coverage gaps + failed members from the
        # loop's own state + the done-check side-channel (artifacts/grade). Never a self-grade.
        dcd = done_check_diag or {}
        return Diagnostic(
            stall_kind=stall_kind,  # type: ignore[arg-type]
            missing_members=[r for r in loop.members if results.get(r) is None],
            failed_members={
                r: member_errors.get(r, "") for r, s in member_status.items() if s == "failed"
            },
            artifacts_landed=dcd.get("artifacts_ok"),
            evaluator_score=dcd.get("evaluator_score"),
            evaluator_floor=dcd.get("evaluator_floor"),
        )

    async def _maybe_recalibrate(stall_kind: str) -> LoopSeamResult | None:
        # #553: on a stall, ONE bounded recalibration BEFORE halting — diagnose (coded), pick ONE
        # directive from the closed set (the model only picks a tactic), apply it (mark the targets
        # not-produced so the coordinator must re-route), and continue. Returns a terminal result to
        # HALT, or None to CONTINUE the loop. Fail-closed: no recalibrator / cap exhausted / an
        # unparseable directive / a repeat directive all halt (no_progress or escalate).
        nonlocal recalibrations_used, last_directive, last_directive_digest
        if recalibrate is None or recalibrations_used >= cap:
            return _result("no_progress")
        directive = await recalibrate(loop, _diagnose(stall_kind))
        if directive is None:
            return _result("no_progress")  # unparseable / router unreachable → halt
        last_directive = directive
        # the directive's EFFECT = the in-loop dispatchable members it actually frees (a gate is
        # re-rendered each round, so targeting one is a no-op; a duplicate / out-of-loop target
        # changes nothing). Anti-repeat keys off THIS normalized effect (sorted, de-duped) so a
        # near-duplicate (re-ordered / duplicated / ghost target) cannot slip the guard.
        targets = sorted(
            {r for r in directive.member_targets if r in loop_roles and r not in gate_roles}
        )
        digest = f"{directive.action}|{','.join(targets)}"
        if directive.action == "escalate" or digest == last_directive_digest:
            return _result("escalate")  # closed-set escalate, or anti-repeat → human
        for role in targets:  # resume over the freed failed/blocked work members (#551)
            results[role] = None
            member_status[role] = "blocked"
        last_directive_digest = digest
        recalibrations_used += 1
        # NB: last_signature is deliberately NOT reset — the re-routed members are re-dispatched
        # next round and a no-op directive re-stalls immediately (1 round, not 2); a real change
        # moves the signature on its own. No ephemeral breadcrumb is written into results (it would
        # leak into the coordinator + the coded done-check, which read the live dict).
        return None  # continue the loop — the next round re-routes to the freed members

    while rounds < max_rounds:
        # bounds are checked FIRST → halt-with-partial, and a blown budget WINS over a pending gate
        if max_wall_seconds is not None and _clock() - started > max_wall_seconds:
            return _result("wall_time")
        if max_cost is not None and _cost() > max_cost:
            return _result("cost_budget")
        # PR-C: the HITL gate CHECK runs before EVERY round — an UNDECIDED gate always PAUSES (no
        # auto-skip; rounds NOT incremented, so resuming re-enters this check idempotently).
        # SEMANTICS: the human's GO is a ONE-TIME approval that applies to the loop (the book's §22
        # GO gate) — once decided in ``gate_decisions`` it stays a GO across rounds, not re-consumed
        # per round. Re-approving every iteration would clear the decision each round — a deliberate
        # non-default (flagged for the use-case-guardian to confirm).
        undecided = [g for g in gate_roles if gates.get(g) is None]
        if undecided:
            return _result("paused", paused_at=undecided)
        for g in gate_roles:  # render the GO once into the loop state (recorded, never dispatched)
            if results.get(g) is None:
                results[g] = {"gate": g, "decision": gates.get(g)}
                member_status[g] = "succeeded"

        next_roles = await coordinate(loop, dict(results), max_rounds - rounds)
        next_roles = list(dict.fromkeys(next_roles))  # PR-C: within-round de-dup — dispatch once
        for role in next_roles:  # fail-closed: route ONLY to declared loop members
            if role not in loop_roles:
                raise OHMError(f"loop coordinator routed to non-loop member {role!r}")
        if not next_roles:
            # the coordinator believes the goal is met — the CODED check decides, not the model.
            if await done_check(results):
                return _result("converged")
            recal = await _maybe_recalibrate("coordinator")  # #553: ONE recalibration before halt
            if recal is not None:
                return recal
            continue  # a directive re-routed the targets — run another round

        rounds += 1
        for role in next_roles:
            member = by_role[role]
            if member.kind == "human":  # a gate is rendered at the round top, never dispatched
                continue
            inbound = _loop_inbound(loop, member, results, by_role, envelopes)
            try:
                out = await dispatch(member, inbound, None)
                results[role] = out
                # #587: mirror the DAG path — a DEGRADED loop member (PARTIAL) is "partial", not
                # "succeeded" (the same degrade-capable dispatch feeds this path).
                member_status[role] = (
                    "partial"
                    if isinstance(out, dict) and out.get("status") == "PARTIAL"
                    else "succeeded"
                )
            except Exception as exc:  # noqa: BLE001 — #551 non-abort: record, never raise out of loop
                results.setdefault(role, None)
                member_status[role] = "failed"
                member_errors[role] = str(exc)[:2000] or type(exc).__name__

        if await done_check(results):  # the coded done-check (coverage-floor + evaluator) confirms
            return _result("converged")
        signature = _progress_signature(results, member_status)  # no-progress: nothing changed
        if signature == last_signature:
            recal = await _maybe_recalibrate("signature")  # #553: ONE recalibration before halt
            if recal is not None:
                return recal
            continue  # a directive re-routed the targets — run another round
        last_signature = signature

    return _result("max_rounds")
