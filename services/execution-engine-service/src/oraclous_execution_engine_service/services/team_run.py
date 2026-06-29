"""Team-run bridge — drive the orchestrator core with the real harness execution (#419 wiring).

ADR-035 §2. Connects ``oraclous_ohm.orchestrate.run_team`` (the dispatch-injected team-DAG executor,
proven in packages/ohm) to the engine's ``HarnessClient``: each member dispatch becomes a harness
execution of that member's generated sub-harness (passed inline). The typed ``HandoffEnvelope``s are
rendered into the harness input — structured, not a flattened 4000-char truncation. A member whose
harness does not SUCCEED fails the team run (fail-closed). Human gates pause the run (ADR-035 §6).

This is the IN-MEMORY bridge; durable persistence of the run state + the gate pauses (so a pause
survives across requests) is the next wiring step on a team-run model + the task board.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.errors import OHMError
from oraclous_ohm.manifest import (
    OHMBudget,
    OHMLoop,
    OHMManifest,
    OHMMember,
    resolve_member_caps,
    resolve_member_on_exhaustion,
)
from oraclous_ohm.orchestrate import (
    Diagnostic,
    DispatchFn,
    DoneCheckFn,
    LoopCoordinateFn,
    LoopSeamResult,
    RecalDirective,
    RecalibrateFn,
    TeamRunResult,
    run_loop_seam,
    run_team,
)

from oraclous_execution_engine_service.services.harness_client import HarnessClientError


class _Harness(Protocol):
    """The slice of ``HarnessClient`` the bridge needs (so a fake satisfies it in tests)."""

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = ...,
        manifest_ref: str | None = ...,
        capability_ceiling: list[str] | None = ...,
        parent_execution_id: uuid.UUID | None = ...,
        trace_id: uuid.UUID | None = ...,
        workspace_root: str | None = ...,
        graph_id: str | None = ...,
        team_id: str | None = ...,
        precedence_order: list[str] | None = ...,
        graph_authoritative: bool = ...,
        max_tokens: int | None = ...,
        max_tool_calls: int | None = ...,
    ) -> dict[str, Any]: ...


# Imported Claude-Code "conductor" agents are written to PROPOSE a `## Handoff` for a human to
# dispatch; run inline inside Oraclous with a thin objective, a model satisfies that persona by
# emitting a handoff stub (no tool calls, no output). This directive re-frames the member as the
# EXECUTOR — do the work now and call its tools — so its in-loop graph-ingest fires and the
# artifacts land on the bound graph (#543 / ADR-041). Compatible with non-imported members (it
# only asks them to use whatever tools they have); fail-soft (it never blocks a legitimately
# tool-less reasoning turn — the loop's completion contract handles acceptance).
EXECUTION_DIRECTIVE = (
    "You are EXECUTING this objective right now inside Oraclous — you are not planning, and "
    "there is no human who will act on a handoff. Do the work yourself and USE YOUR TOOLS to do "
    "it: your Write tool persists your output to the team's shared knowledge graph (this is how "
    "your work is saved and made visible to the rest of the team); your Read tool retrieves what "
    "other members have already written there. Produce your substantive output and persist it "
    "with your Write tool before you finish. A reply that only proposes a '## Handoff' or a next "
    "step, without doing the work and calling your tools, is NOT an acceptable result."
)


def render_member_input(
    member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any = None
) -> str:
    """Render a member's objective + fan item + inbound typed hand-offs into the harness input."""
    parts: list[str] = []
    # #577: the inbound handoff's objective_slice scopes THIS dispatch (the producer's ## Handoff
    # Next-task — e.g. "Draft Chapter 04") and takes precedence over the member's static subgoal
    # (e.g. "draft a chapter"); falls back to the subgoal when no inbound handoff carries one. This
    # is what makes a consumer act on its per-edge objective instead of a generic blurb.
    # Single-producer-per-consumer assumption: a FAN-IN consumer takes the FIRST inbound objective
    # (depends_on order); per-producer objective composition is out of scope for this slice (the
    # targeted pipeline artifacts — bitcoin's ## Handoff chain, the book charters — have one handoff
    # producer per consumer). Every producer's PAYLOAD still reaches the member via the From-lines.
    scoped = next((e.objective_slice for e in envelopes if e.objective_slice), "")
    objective = scoped or member.subgoal
    if objective:
        parts.append(f"Objective: {objective}")
    if fan_item is not None:
        parts.append(f"Item: {json.dumps(fan_item, default=str)}")
    for env in envelopes:
        parts.append(f"From {env.from_role}: {json.dumps(env.payload, default=str)}")
    parts.append(EXECUTION_DIRECTIVE)
    return "\n\n".join(parts)


def make_harness_dispatch(
    harness: _Harness,
    sub_harnesses: dict[str, dict[str, Any]],
    *,
    trace_id: uuid.UUID | None = None,
    parent_execution_id: uuid.UUID | None = None,
    on_child: Callable[[str], None] | None = None,
    on_cost: Callable[[int], None] | None = None,
    workspace_root: str | None = None,
    graph_id: str | None = None,
    team_id: str | None = None,
    precedence_order: list[str] | None = None,
    graph_authoritative: bool = False,
    budget: OHMBudget | None = None,
) -> DispatchFn:
    """Build a ``run_team`` dispatch that runs each member as a real harness execution.

    Run-tree correlation (#471): ``trace_id`` (the team-run root) + ``parent_execution_id`` are
    threaded into every member's harness run so the harness stamps each into the same tree; each
    member's harness execution id is surfaced via ``on_child`` so the engine records the tree.
    O4 metering (#472): each member's ``total_tokens`` is surfaced via ``on_cost`` so the engine
    accumulates the run's RAW token cost from the harness's own metering (ADR-009)."""

    async def dispatch(member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any) -> Any:
        sub = sub_harnesses.get(member.role)
        # #576: the member's user-set runtime SAFETY CAP (member override > team-wide default,
        # clamped <= the team-pooled total when a budget is present). Sent whenever a cap RESOLVES —
        # a member's OWN max_tokens binds with no team budget; only a team with NEITHER a member cap
        # nor a budget adds zero kwargs and runs unchanged (the tier stands). The harness applies
        # the cap as the per-member token / tool-call ceiling.
        member_max_tokens, member_max_tool_calls = resolve_member_caps(member, budget)
        caps: dict[str, Any] = {}
        if member_max_tokens is not None:
            caps["max_tokens"] = member_max_tokens
        if member_max_tool_calls is not None:
            caps["max_tool_calls"] = member_max_tool_calls
        # #587: the member's resolved on_exhaustion (member-over-team) rides to the harness like the
        # caps. Sent ONLY for the explicit "degrade" — "escalate" is the harness default, so an
        # unchanged team adds zero kwargs (the #576 send-only-when-set pattern; back-compat).
        if resolve_member_on_exhaustion(member, budget) == "degrade":
            caps["on_exhaustion"] = "degrade"
        result = await harness.execute(
            input_text=render_member_input(member, envelopes, fan_item),
            manifest_inline=sub,
            manifest_ref=(member.manifest_ref if sub is None else None),
            # the member's tools[] is the authoritative ceiling (ADR-032/035 §5) — it caps the
            # harness fail-closed for BOTH the inline AND the manifest_ref path, so a registered
            # manifest_ref harness can never exceed what the member declared (red-team G-A).
            capability_ceiling=list(member.tools),
            **caps,
            parent_execution_id=parent_execution_id,
            trace_id=trace_id,
            # file-native blackboard (#518): the trusted per-run working tree every member's file
            # tools operate on in place (the harness sets it on each file-tool instance's config).
            workspace_root=workspace_root,
            # graph substrate (#524): the per-run graph the graph tools (knowledge-retriever /
            # graph-ingest / find-similar) target — set on each instance's config so the model
            # never invents a UUID. org-scoped at create (cross-org rejected).
            graph_id=graph_id,
            # team-scope blackboard (#513): the stable team identity (team-manifest id) every member
            # shares — the harness writes/reads team-scope memory under it, so concurrent members +
            # future runs of the same team see one blackboard (the adopted-graph world-model).
            team_id=team_id,
            # Hierarchy of Truth (#538): the team's precedence + authoritative flag, bound onto
            # each knowledge-retriever instance so a member's in-loop read is auto-ranked (#514).
            precedence_order=precedence_order,
            graph_authoritative=graph_authoritative,
        )
        status = result.get("status")
        # run-tree (#471): record the child execution id + token cost BEFORE the fail-closed check,
        # so a FAILED member is still surfaced in GET /tree (not an empty []) and its tokens still
        # count. Skipped if the harness omitted an id.
        child_id = result.get("id")
        if on_child is not None and child_id is not None:
            on_child(str(child_id))
        # O4 metering (#472): accumulate this member's RAW token cost (0 if the harness omitted it)
        if on_cost is not None:
            on_cost(int(result.get("total_tokens") or 0))
        # #587: PARTIAL (on_exhaustion=degrade) is a GOVERNED graceful exhaustion — a flagged
        # partial member result, NOT a failure. It must NOT raise (only a genuine FAILED does); the
        # orchestrator records it "partial" and the team is not cascade-failed by a degrade.
        if status not in ("SUCCEEDED", "PARTIAL"):  # fail-closed — surface the REAL harness error
            detail = result.get("error_message") or result.get("error_type")
            raise HarnessClientError(
                f"member {member.role!r} harness did not succeed: {status}"
                + (f" — {detail}" if detail else "")
            )
        return {"output": result.get("output"), "status": status}

    return dispatch


async def run_team_harness(
    manifest: OHMManifest,
    harness: _Harness,
    *,
    sub_harnesses: dict[str, dict[str, Any]] | None = None,
    gate_decisions: dict[str, str] | None = None,
    completed: dict[str, Any] | None = None,
    trace_id: uuid.UUID | None = None,
    parent_execution_id: uuid.UUID | None = None,
    on_child: Callable[[str], None] | None = None,
    on_cost: Callable[[int], None] | None = None,
    cost_so_far: Callable[[], int] | None = None,
    workspace_root: str | None = None,
    graph_id: str | None = None,
    inputs: dict[str, Any] | None = None,
    precedence_order: list[str] | None = None,
    graph_authoritative: bool = False,
) -> TeamRunResult:
    """Run a Team Harness member DAG, dispatching each member as a real harness execution.

    ``completed`` (members that already ran in a prior drive) is passed through so a resume past a
    human gate does not re-dispatch already-finished members (their side effects fire once).
    ``trace_id``/``parent_execution_id``/``on_child`` thread + collect the run-tree (#471);
    ``on_cost`` accumulates the run's RAW token cost (#472)."""
    # team-scope blackboard (#513): the STABLE team identity is the team-manifest id — derived here
    # (not a separate binding) + threaded to every member so they share one team-scope memory.
    team_id = str(manifest.metadata.id)
    # #585: the running pooled tally feeds run_team's pre-dispatch pooled ceiling gate (ADR-031 D3).
    # Prefer the CALLER's cost_so_far (the engine's tally — it includes prior_cost across a resume,
    # so a resumed run cannot re-spend past the ceiling); else build it from THIS drive's on_cost
    # (the direct/unit path). The caller's on_cost (the DB cost_tokens accumulator) still fires.
    cost_deltas: list[int] = []

    def _on_cost(tokens: int) -> None:
        cost_deltas.append(tokens)
        if on_cost is not None:
            on_cost(tokens)

    pooled_cost = cost_so_far if cost_so_far is not None else (lambda: sum(cost_deltas))
    dispatch = make_harness_dispatch(
        harness,
        sub_harnesses or {},
        trace_id=trace_id,
        parent_execution_id=parent_execution_id,
        on_child=on_child,
        on_cost=_on_cost,
        workspace_root=workspace_root,
        graph_id=graph_id,
        team_id=team_id,
        precedence_order=precedence_order,
        graph_authoritative=graph_authoritative,
        budget=manifest.budget,  # #576: per-member caps resolve from the team budget + members
    )
    return await run_team(
        manifest,
        dispatch,
        state=inputs,  # #599: user-seeded state for a member's fan_out.over: "$.<key>"
        gate_decisions=gate_decisions,
        completed=completed,
        cost_so_far=pooled_cost,
    )


# A genuine loop (ADR-043 #552) is interleaved into the acyclic skeleton as ONE condensed node under
# this synthetic role, so ``run_team`` schedules it at its topological position (its downstream
# members run only AFTER it). The node's dispatch expands to the bounded ``run_loop_seam``.
_LOOP_NODE_PREFIX = "__loop__"
_DEFAULT_MAX_ROUNDS = 20  # the conductor's round cap when the manifest declares no max_rounds


def _loop_node_role(index: int) -> str:
    return f"{_LOOP_NODE_PREFIX}{index}"


def _loop_node_index(role: str) -> int | None:
    if role.startswith(_LOOP_NODE_PREFIX):
        try:
            return int(role[len(_LOOP_NODE_PREFIX) :])
        except ValueError:
            return None
    return None


def _condense(
    manifest: OHMManifest, loops: list[OHMLoop]
) -> tuple[list[OHMMember], dict[str, int]]:
    """Build the condensed member DAG: the acyclic skeleton + ONE synthetic node per loop, with
    every ``depends_on`` that points INTO a loop re-pointed to that loop's synthetic node. The
    synthetic node's own ``depends_on`` is the loop's inter-SCC upstream (the importer already
    stripped the intra-loop edges). Pure; the condensed graph is acyclic iff the inter-SCC graph is
    (which the importer guarantees), so ``run_team`` topologically orders it."""
    loop_of_role = {role: i for i, loop in enumerate(loops) for role in loop.members}

    def repoint(deps: list[str]) -> list[str]:
        # a dep into a loop member becomes a dep on that loop's node (de-duplicated, stable order)
        return sorted({_loop_node_role(loop_of_role[d]) if d in loop_of_role else d for d in deps})

    skeleton = [
        m.model_copy(update={"depends_on": repoint(m.depends_on)})
        for m in manifest.skeleton_members()
    ]
    by_role = {m.role: m for m in manifest.members}
    synthetic: list[OHMMember] = []
    for i, loop in enumerate(loops):
        upstream: set[str] = set()
        for role in loop.members:
            for dep in by_role[role].depends_on:
                if dep not in loop.members:  # an inter-SCC upstream edge (intra were stripped)
                    upstream.add(_loop_node_role(loop_of_role[dep]) if dep in loop_of_role else dep)
        synthetic.append(
            OHMMember(
                role=_loop_node_role(i),
                kind="agent",
                manifest_ref="internal:loop-conductor",  # intercepted — never a harness dispatch
                depends_on=sorted(upstream),
            )
        )
    return skeleton + synthetic, loop_of_role


async def run_team_hybrid(
    manifest: OHMManifest,
    harness: _Harness,
    *,
    coordinate: LoopCoordinateFn | None = None,
    done_check_for: Callable[[OHMLoop, dict[str, Any]], DoneCheckFn] | None = None,
    recalibrate: RecalibrateFn | None = None,
    cost_so_far: Callable[[], int] | None = None,
    sub_harnesses: dict[str, dict[str, Any]] | None = None,
    gate_decisions: dict[str, str] | None = None,
    completed: dict[str, Any] | None = None,
    loop_state: dict[str, Any] | None = None,
    trace_id: uuid.UUID | None = None,
    parent_execution_id: uuid.UUID | None = None,
    on_child: Callable[[str], None] | None = None,
    on_cost: Callable[[int], None] | None = None,
    workspace_root: str | None = None,
    graph_id: str | None = None,
    inputs: dict[str, Any] | None = None,
    precedence_order: list[str] | None = None,
    graph_authoritative: bool = False,
) -> TeamRunResult:
    """Drive a Team Harness whose handoff graph has GENUINE loops (ADR-043 #552): the acyclic
    skeleton runs on ``run_team`` and each loop SCC runs the bounded ``run_loop_seam`` conductor,
    interleaved at its topological position via a condensed node. Upstream→loop→downstream data
    flows through the shared graph/blackboard (every member shares ``graph_id``), so the hybrid only
    has to ORDER the loops correctly. A purely acyclic team (no loops) is delegated unchanged to
    ``run_team_harness``.

    ``coordinate`` (picks the next loop member) + ``done_check_for`` (the CODED done-check per loop)
    are INJECTED — the engine wires the real BYOM coordinator + coverage/artifacts/evaluator check;
    a loop team with either unwired FAILS CLOSED (the team can never satisfy its own done-check).
    A loop that does not converge raises out of its condensed node, so ``run_team`` records it
    failed + BLOCKS its downstream (#551 non-abort), and the run is re-runnable."""
    loops = list(manifest.orchestration.loops) if manifest.orchestration else []
    if not loops:  # purely acyclic — the unchanged single-pass DAG path
        return await run_team_harness(
            manifest,
            harness,
            sub_harnesses=sub_harnesses,
            gate_decisions=gate_decisions,
            completed=completed,
            trace_id=trace_id,
            parent_execution_id=parent_execution_id,
            on_child=on_child,
            on_cost=on_cost,
            cost_so_far=cost_so_far,  # #585: the engine's pooled tally (incl. prior_cost on resume)
            workspace_root=workspace_root,
            graph_id=graph_id,
            inputs=inputs,  # #599: user-seeded state for a fan_out.over: "$.<key>"
            precedence_order=precedence_order,
            graph_authoritative=graph_authoritative,
        )
    if coordinate is None or done_check_for is None:  # fail-closed (ADR-043 invariant)
        raise OHMError("team has loops but no coordinator/done-check wired")

    by_role = {m.role: m for m in manifest.members}
    team_id = str(manifest.metadata.id)
    real_dispatch = make_harness_dispatch(
        harness,
        sub_harnesses or {},
        trace_id=trace_id,
        parent_execution_id=parent_execution_id,
        on_child=on_child,
        on_cost=on_cost,
        workspace_root=workspace_root,
        graph_id=graph_id,
        team_id=team_id,
        precedence_order=precedence_order,
        graph_authoritative=graph_authoritative,
        budget=manifest.budget,  # #576: per-member caps resolve from the team budget + members
    )
    termination = manifest.orchestration.termination if manifest.orchestration else None
    max_rounds = (termination.max_rounds if termination else None) or _DEFAULT_MAX_ROUNDS
    max_wall = termination.max_wall_seconds if termination else None
    max_cost = manifest.budget.max_tokens_total if manifest.budget else None

    condensed, _ = _condense(manifest, loops)
    loop_results: dict[int, LoopSeamResult] = {}
    in_loop_state = loop_state or {}  # PR-C: prior per-loop checkpoint (resume), by loop index
    out_loop_state: dict[str, Any] = {}  # PR-C: the checkpoint to persist after this drive
    paused_gates: list[str] = []  # PR-C: per-round HITL gate(s) a loop is paused on

    async def hybrid_dispatch(
        member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any
    ) -> Any:
        i = _loop_node_index(member.role)
        if i is None:  # an ordinary skeleton member — the real harness dispatch
            return await real_dispatch(member, envelopes, fan_item)
        loop = loops[i]
        # seed the loop with any members already delivered in a prior drive (resume / re-run)
        seed = {r: completed[r] for r in loop.members if completed and r in completed}
        # PR-C: resume the round-index + the ORIGINAL wall-clock start ONLY for a PAUSED loop (a
        # mid-loop HITL suspension — continue where it left off; the epoch started_at survives the
        # process restart across a long pause so the wall-clock measures real elapsed time). A loop
        # that halted at a BOUND (max_rounds / wall / cost / no_progress) is a spent attempt — an
        # ADR-042 re-run must RESTART it (round 0, fresh wall-clock), not resume the spent round.
        cp = in_loop_state.get(str(i), {})
        if cp.get("status") == "paused":
            resume_round = int(cp.get("round") or 0)
            started = float(cp["started_at"]) if cp.get("started_at") is not None else time.time()
            # #553: the recalibration COUNT survives a HITL pause/approve cycle so the cap holds
            # across resume (a constant cap=1 is itself stable; only the spent count must persist).
            # NB the anti-repeat DIGEST is intentionally NOT persisted: at cap=1 a 2nd recalibration
            # (where the digest would be compared) never occurs — the cap halts first. If the cap is
            # ever raised, also persist + thread ``resume_last_directive_digest`` here.
            resume_recals = int(cp.get("recalibration_count") or 0)
        else:
            resume_round, started, resume_recals = 0, time.time(), 0
        # #553: the coded done-check writes WHICH gate failed (artifacts / grade) into this shared
        # side-channel; the seam reads it to build the (coded, external) recalibration Diagnostic.
        done_check_diag: dict[str, Any] = {}
        seam = await run_loop_seam(
            loop,
            by_role,
            real_dispatch,
            coordinate,  # type: ignore[arg-type]  # narrowed non-None above
            done_check_for(loop, done_check_diag),  # type: ignore[misc]
            max_rounds=max_rounds,
            max_wall_seconds=max_wall,
            max_cost=max_cost,
            cost_so_far=cost_so_far,
            seed_results=seed or None,
            gate_decisions=gate_decisions,
            resume_from_round=resume_round,
            started_at=started,
            clock=time.time,
            recalibrate=recalibrate,  # #553: None for a non-loop drive → the seam is byte-unchanged
            recalibration_cap=_RECALIBRATION_CAP,
            done_check_diag=done_check_diag,
            resume_recalibrations_used=resume_recals,
        )
        loop_results[i] = seam
        out_loop_state[str(i)] = {
            "round": seam.rounds,
            "started_at": started,
            "status": seam.status,
            "recalibration_count": seam.recalibrations_used,  # #553: persist for resume cap enforce
        }
        if seam.status == "paused":  # PR-C: a per-round HITL gate awaits a human decision
            paused_gates.extend(seam.paused_at)
        if seam.status != "converged":  # paused OR a bound halt → block downstream (#551 non-abort)
            raise HarnessClientError(f"loop {i}: {seam.status}")
        return {"loop": i, "status": seam.status, "output": seam.results}

    skeleton = await run_team(
        manifest,
        hybrid_dispatch,
        state=inputs,  # #599: user-seeded state for a skeleton member's fan_out.over: "$.<key>"
        gate_decisions=gate_decisions,
        completed=completed,
        members=condensed,
        cost_so_far=cost_so_far,  # #585: the pooled token gate binds the skeleton members too
    )

    # merge each loop's real-member results into the team result; the synthetic node is internal
    for i, seam in loop_results.items():
        skeleton.results.update(seam.results)
        skeleton.member_status.update(seam.member_status)
        skeleton.member_errors.update(seam.member_errors)
        skeleton.envelopes.extend(seam.envelopes)
        if seam.status == "paused":
            continue  # PR-C: a PAUSED loop is NOT a failure — its members are not marked failed
        # a non-converged loop FAILED AS A UNIT — its goal was not met though its members each
        # dispatched. Mark EVERY loop member failed so the run is re-runnable (re-drives the loop,
        # ADR-042 #551); a member that genuinely raised in-loop keeps its own leak-safe error.
        if seam.status != "converged":
            for role in loops[i].members:
                skeleton.member_status[role] = "failed"
                skeleton.member_errors.setdefault(role, f"loop did not converge: {seam.status}")
    for i in loop_results:  # drop the internal condensed node from the surfaced results/status
        skeleton.results.pop(_loop_node_role(i), None)
        skeleton.member_status.pop(_loop_node_role(i), None)
        skeleton.member_errors.pop(_loop_node_role(i), None)
    skeleton.loop_state = out_loop_state  # PR-C: the per-loop checkpoint for the engine to persist
    # PR-C: a per-round HITL gate PAUSES the team (awaiting the human decision) — not a failure; the
    # advance machinery resumes it. Pause takes precedence over a run_team-marked blocked member.
    if paused_gates:
        skeleton.status = "paused"
        skeleton.paused_at = sorted(set(skeleton.paused_at) | set(paused_gates))
        return skeleton
    # the team SUCCEEDS only when every member delivered (ADR-042); any failed/blocked → FAILED
    if any(s in ("failed", "blocked") for s in skeleton.member_status.values()):
        skeleton.status = "failed"
    return skeleton


# ── the BYOM loop coordinator (ADR-043 #552) ───────────────────────────────────────────────────
# The conductor's router: a bounded model turn that ONLY PICKS the next loop member to run. It NEVER
# decides "done" (the coded done-check does) and NEVER grants a capability (capability_ceiling=[]).
# LEAK-SAFETY: the prompt carries the loop's STRUCTURE — each member's role, its ## Handoff routing
# intent, and a produced/not-produced boolean — but NEVER a member's raw output (customer text). The
# CONTENT judgement (has the work met the bar?) is the separate coded evaluator's job, not the
# router's, so no runtime output is ever re-emitted into a model prompt or a log.


def _render_coordinator_prompt(
    loop: OHMLoop, results: dict[str, Any], rounds_left: int, members: list[str] | None = None
) -> str:
    """The coordinator's input — loop structure + a per-member produced flag (NO raw outputs).
    ``members`` (default = every loop member) is the WORK members the router may pick — a per-round
    HITL gate (kind:human) is excluded (the seam handles it), so the router never routes to it."""
    roles = members if members is not None else list(loop.members)
    lines = [
        "You are the COORDINATOR of a team loop. Pick the SINGLE next member to run so the loop",
        "makes progress toward its goal. You do NOT decide when the loop is done and you do NOT do",
        "any member's work — you only route.",
        "",
        f"Rounds left before the loop is force-stopped: {rounds_left}.",
        "Members of this loop (role — its handoff intent — has it produced yet?):",
    ]
    for role in roles:
        intent = loop.routing.get(role, "") or "(no stated intent)"
        produced = "produced" if results.get(role) is not None else "not yet produced"
        lines.append(f"  - {role} — {intent} — {produced}")
    lines += [
        "",
        "Reply with ONLY the role name of the next member to run (exactly as written above).",
        "If you believe the loop's goal is met, reply with the single word DONE — a separate coded",
        "check will confirm or send the loop back to you. Reply with nothing else.",
    ]
    return "\n".join(lines)


def _parse_next_roles(output: Any, *, allowed: set[str]) -> list[str]:
    """Parse the coordinator's reply into the next loop member(s), FAIL-CLOSED. Only declared loop
    members survive (a hallucinated outsider becomes a no-op pick, never a seam abort); DONE / empty
    / unparseable → ``[]`` (the coded done-check then decides). Never logs the model output."""
    if not isinstance(output, str):
        return []
    text = output.strip()
    if not text or text.upper().startswith("DONE"):
        return []
    # accept a bare role, a quoted role, or a leading token; match against declared members only
    picks: list[str] = []
    for token in text.replace(",", " ").replace("\n", " ").split():
        cleaned = token.strip().strip("\"'`.").strip()
        if cleaned in allowed and cleaned not in picks:
            picks.append(cleaned)
    return picks


def _coordinator_subharness(team: OHMManifest) -> dict[str, Any]:
    """A tool-LESS single-agent harness for the coordinator turn (picks-only — no ``capabilities``).
    Binds the team's coordinator BYOM model (role ``coordinator`` → ``evaluator`` → ``primary``), so
    the router runs on the user's own key through the gateway (ADR-008); none declared → the harness
    falls back to the operator model."""
    model = team.model_by_role("coordinator") or team.evaluator_model() or team.primary_model()
    doc: dict[str, Any] = {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "loop-coordinator",
            "owner_organization_id": str(team.metadata.owner_organization_id),
        },
        "capabilities": [],  # picks-only — the router can call NO tool (ADR-043 invariant)
        "prompts": [
            {
                "role": "primary",
                "source": "inline",
                "body": "You route a team loop. Follow the user instruction exactly; reply with "
                "only a role name or DONE.",
            }
        ],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }
    if model is not None:
        doc["models"] = [model.model_dump(mode="json")]
    return doc


def make_loop_coordinator(harness: _Harness, team: OHMManifest) -> LoopCoordinateFn:
    """Build the BYOM loop coordinator (ADR-043 #552) — a bounded, tool-less model turn that picks
    the next loop member. Picks-only (``capability_ceiling=[]``), leak-safe (structure not content),
    fail-closed (an unreachable/garbled router yields ``[]`` → the coded done-check rules)."""
    sub = _coordinator_subharness(team)
    by_role = {m.role: m for m in team.members}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        # PR-C: route ONLY among work members — a kind:human per-round gate is a structural pause
        # (the seam pauses/renders it), never a coordinator pick, so it can't waste a round.
        work = [r for r in loop.members if not (by_role.get(r) and by_role[r].kind == "human")]
        try:
            out = await harness.execute(
                input_text=_render_coordinator_prompt(loop, results, rounds_left, members=work),
                manifest_inline=sub,
                capability_ceiling=[],  # the router is granted NO capability
            )
        except HarnessClientError:
            return []  # router unreachable → give up this round; the coded done-check decides
        return _parse_next_roles(out.get("output"), allowed=set(work))

    return coordinate


# ── ADR-043 #553: bounded recalibration — the BYOM directive turn ──────────────────────────────
# Engine default cap: ONE recovery attempt before halting (ADR-043: 1-2, max 3). A constant (no
# manifest field), so the cap is stable across a HITL resume; only the recalibration COUNT persists.
_RECALIBRATION_CAP = 1
_RECAL_ACTIONS = ("re-plan", "re-frame-objective", "change-strategy", "re-scope-member", "escalate")


def _recalibration_subharness(team: OHMManifest) -> dict[str, Any]:
    """A tool-LESS single-agent harness for the recalibration turn — the model PICKS one tactic from
    the closed set given a CODED diagnosis (it never diagnoses itself). Same BYOM model + zero
    capabilities as the coordinator (ADR-008 / ADR-043 invariant)."""
    doc = _coordinator_subharness(team)
    doc["metadata"]["name"] = "loop-recalibrator"
    doc["prompts"][0]["body"] = (
        "You recalibrate a STALLED team loop. Given a coded diagnosis, reply with ONE action token "
        "from the allowed set followed by the member roles to retry. Reply with nothing else."
    )
    return doc


def _render_recalibration_prompt(loop: OHMLoop, diag: Diagnostic, work: list[str]) -> str:
    """The recalibrator's input — the CODED, external diagnosis (NO raw member outputs) + the closed
    action menu. Leak-safe: only role names + coverage/grade signals, never any produced content."""
    lines = [
        "A team loop has STALLED (it stopped making progress). Diagnosis (coded, external):",
        f"  - stall kind: {diag.stall_kind}",
        f"  - members not yet produced: {', '.join(diag.missing_members) or '(none)'}",
        f"  - members that FAILED: {', '.join(diag.failed_members) or '(none)'}",
    ]
    if diag.artifacts_landed is not None:
        lines.append(f"  - work persisted to the graph: {'yes' if diag.artifacts_landed else 'no'}")
    if diag.evaluator_score is not None:
        floor = diag.evaluator_floor if diag.evaluator_floor is not None else "?"
        lines.append(f"  - evaluator grade: {diag.evaluator_score} (needs >= {floor})")
    lines += [
        "",
        "Pick ONE recovery action from this CLOSED set:",
        "  - re-plan — redo the approach, same objective",
        "  - re-frame-objective — restate the goal more concretely",
        "  - change-strategy — try a different method",
        "  - re-scope-member — narrow a member's task",
        "  - escalate — give up and ask a human (only when truly stuck)",
        "",
        "Members you may retry: " + (", ".join(work) or "(none)"),
        "Reply with ONLY the action token then the roles to retry (space-separated).",
        "Example: change-strategy " + (work[0] if work else "member"),
    ]
    return "\n".join(lines)


def _parse_recalibration_output(output: Any, *, allowed: set[str]) -> RecalDirective:
    """Parse the recalibrator's reply into ONE directive, FAIL-CLOSED: an unparseable / empty reply,
    NO recognised action, an explicit ``escalate``, OR an AMBIGUOUS reply (two different actions) →
    ``escalate`` (never a silent no-op retry, never first-token-wins over a hedged escalate). Only
    declared work members survive as targets (a hallucinated outsider, and the matched action token
    itself, are dropped). Never logs the model output."""
    if not isinstance(output, str) or not output.strip():
        return RecalDirective(action="escalate", reason="unparseable")
    raw = [t.strip().strip("\"'`.") for t in output.replace(",", " ").split()]
    # normalise each token to the canonical hyphenated form (models emit re_plan / RE-PLAN / …)
    norm = [t.lower().replace("_", "-") for t in raw]
    actions = list(dict.fromkeys(t for t in norm if t in _RECAL_ACTIONS))  # distinct, in order
    # fail-closed: no action, an explicit escalate, OR ambiguity (>1 distinct action) → escalate
    if not actions or "escalate" in actions or len(actions) > 1:
        reason = "no_action" if not actions else "ambiguous_or_escalate"
        return RecalDirective(action="escalate", reason=reason)
    action = actions[0]
    # targets = declared work members, EXCLUDING the matched action token (a role named like an
    # action can't double as both — the collision the closed set would otherwise hide), de-duped
    targets = list(
        dict.fromkeys(r for r, n in zip(raw, norm, strict=True) if r in allowed and n != action)
    )
    return RecalDirective(action=action, reason="byom", member_targets=targets)  # type: ignore[arg-type]


def make_recalibration_coordinator(harness: _Harness, team: OHMManifest) -> RecalibrateFn:
    """Build the BYOM recalibrator (ADR-043 #553) — a bounded, tool-less model turn that, on a loop
    stall, PICKS one tactic from the closed action set given a CODED diagnosis (it never diagnoses
    itself; the model only chooses, the coded done-check still rules). Leak-safe (structure + coded
    signals, never content), fail-closed (an unreachable/garbled router yields ``escalate`` or
    ``None`` → halt-to-human, never a silent retry)."""
    sub = _recalibration_subharness(team)
    by_role = {m.role: m for m in team.members}

    async def recalibrate(loop: OHMLoop, diag: Diagnostic) -> RecalDirective | None:
        # retry only WORK members — a kind:human gate is re-rendered by the seam, never retried
        work = [r for r in loop.members if not (by_role.get(r) and by_role[r].kind == "human")]
        try:
            out = await harness.execute(
                input_text=_render_recalibration_prompt(loop, diag, work),
                manifest_inline=sub,
                capability_ceiling=[],  # the recalibrator is granted NO capability
            )
        except HarnessClientError:
            return None  # router unreachable → halt fail-closed (no_progress), never a silent retry
        return _parse_recalibration_output(out.get("output"), allowed=set(work))

    return recalibrate
