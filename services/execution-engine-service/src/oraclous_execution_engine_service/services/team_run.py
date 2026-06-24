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
from oraclous_ohm.manifest import OHMManifest, OHMMember
from oraclous_ohm.orchestrate import DispatchFn, TeamRunResult, run_team

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
    ) -> dict[str, Any]: ...


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
        )
        status = result.get("status")
        if status != "SUCCEEDED":  # fail-closed — a member's harness must succeed
            raise HarnessClientError(f"member {member.role!r} harness did not succeed: {status}")
        # run-tree (#471): record the child execution id so the engine reassembles the tree from its
        # own record (no cross-DB read into the harness). Skipped if the harness omitted an id.
        child_id = result.get("id")
        if on_child is not None and child_id is not None:
            on_child(str(child_id))
        # O4 metering (#472): accumulate this member's RAW token cost (0 if the harness omitted it)
        if on_cost is not None:
            on_cost(int(result.get("total_tokens") or 0))
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
    )
    return await run_team(manifest, dispatch, gate_decisions=gate_decisions, completed=completed)
