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
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.errors import OHMError
from oraclous_ohm.manifest import OHMLoop, OHMManifest, OHMMember
from oraclous_ohm.orchestrate import (
    DispatchFn,
    DoneCheckFn,
    LoopCoordinateFn,
    LoopSeamResult,
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
    if member.subgoal:
        parts.append(f"Objective: {member.subgoal}")
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
) -> DispatchFn:
    """Build a ``run_team`` dispatch that runs each member as a real harness execution.

    Run-tree correlation (#471): ``trace_id`` (the team-run root) + ``parent_execution_id`` are
    threaded into every member's harness run so the harness stamps each into the same tree; each
    member's harness execution id is surfaced via ``on_child`` so the engine records the tree.
    O4 metering (#472): each member's ``total_tokens`` is surfaced via ``on_cost`` so the engine
    accumulates the run's RAW token cost from the harness's own metering (ADR-009)."""

    async def dispatch(member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any) -> Any:
        sub = sub_harnesses.get(member.role)
        result = await harness.execute(
            input_text=render_member_input(member, envelopes, fan_item),
            manifest_inline=sub,
            manifest_ref=(member.manifest_ref if sub is None else None),
            # the member's tools[] is the authoritative ceiling (ADR-032/035 §5) — it caps the
            # harness fail-closed for BOTH the inline AND the manifest_ref path, so a registered
            # manifest_ref harness can never exceed what the member declared (red-team G-A).
            capability_ceiling=list(member.tools),
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
        if status != "SUCCEEDED":  # fail-closed — surface the REAL harness error, not a bare status
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
    workspace_root: str | None = None,
    graph_id: str | None = None,
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
    dispatch = make_harness_dispatch(
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
    )
    return await run_team(manifest, dispatch, gate_decisions=gate_decisions, completed=completed)


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
    done_check_for: Callable[[OHMLoop], DoneCheckFn] | None = None,
    cost_so_far: Callable[[], int] | None = None,
    sub_harnesses: dict[str, dict[str, Any]] | None = None,
    gate_decisions: dict[str, str] | None = None,
    completed: dict[str, Any] | None = None,
    trace_id: uuid.UUID | None = None,
    parent_execution_id: uuid.UUID | None = None,
    on_child: Callable[[str], None] | None = None,
    on_cost: Callable[[int], None] | None = None,
    workspace_root: str | None = None,
    graph_id: str | None = None,
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
            workspace_root=workspace_root,
            graph_id=graph_id,
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
    )
    termination = manifest.orchestration.termination if manifest.orchestration else None
    max_rounds = (termination.max_rounds if termination else None) or _DEFAULT_MAX_ROUNDS
    max_wall = termination.max_wall_seconds if termination else None
    max_cost = manifest.budget.max_tokens_total if manifest.budget else None

    condensed, _ = _condense(manifest, loops)
    loop_results: dict[int, LoopSeamResult] = {}

    async def hybrid_dispatch(
        member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any
    ) -> Any:
        i = _loop_node_index(member.role)
        if i is None:  # an ordinary skeleton member — the real harness dispatch
            return await real_dispatch(member, envelopes, fan_item)
        loop = loops[i]
        # seed the loop with any members already delivered in a prior drive (resume / re-run)
        seed = {r: completed[r] for r in loop.members if completed and r in completed}
        seam = await run_loop_seam(
            loop,
            by_role,
            real_dispatch,
            coordinate,  # type: ignore[arg-type]  # narrowed non-None above
            done_check_for(loop),  # type: ignore[misc]
            max_rounds=max_rounds,
            max_wall_seconds=max_wall,
            max_cost=max_cost,
            cost_so_far=cost_so_far,
            seed_results=seed or None,
        )
        loop_results[i] = seam
        if seam.status != "converged":  # a non-converged loop fails its node → downstream BLOCKED
            raise HarnessClientError(f"loop {i} did not converge: {seam.status}")
        return {"loop": i, "status": seam.status, "output": seam.results}

    skeleton = await run_team(
        manifest,
        hybrid_dispatch,
        gate_decisions=gate_decisions,
        completed=completed,
        members=condensed,
    )

    # merge each loop's real-member results into the team result; the synthetic node is internal
    for i, seam in loop_results.items():
        skeleton.results.update(seam.results)
        skeleton.member_status.update(seam.member_status)
        skeleton.member_errors.update(seam.member_errors)
        skeleton.envelopes.extend(seam.envelopes)
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


def _render_coordinator_prompt(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> str:
    """The coordinator's input — loop structure + a per-member produced flag (NO raw outputs)."""
    lines = [
        "You are the COORDINATOR of a team loop. Pick the SINGLE next member to run so the loop",
        "makes progress toward its goal. You do NOT decide when the loop is done and you do NOT do",
        "any member's work — you only route.",
        "",
        f"Rounds left before the loop is force-stopped: {rounds_left}.",
        "Members of this loop (role — its handoff intent — has it produced yet?):",
    ]
    for role in loop.members:
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

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        try:
            out = await harness.execute(
                input_text=_render_coordinator_prompt(loop, results, rounds_left),
                manifest_inline=sub,
                capability_ceiling=[],  # the router is granted NO capability
            )
        except HarnessClientError:
            return []  # router unreachable → give up this round; the coded done-check decides
        return _parse_next_roles(out.get("output"), allowed=set(loop.members))

    return coordinate
