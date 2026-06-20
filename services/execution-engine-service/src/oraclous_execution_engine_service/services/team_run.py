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
    harness: _Harness, sub_harnesses: dict[str, dict[str, Any]]
) -> DispatchFn:
    """Build a ``run_team`` dispatch that runs each member as a real harness execution."""

    async def dispatch(member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any) -> Any:
        sub = sub_harnesses.get(member.role)
        result = await harness.execute(
            input_text=render_member_input(member, envelopes, fan_item),
            manifest_inline=sub,
            manifest_ref=(member.manifest_ref if sub is None else None),
        )
        status = result.get("status")
        if status != "SUCCEEDED":  # fail-closed — a member's harness must succeed
            raise HarnessClientError(f"member {member.role!r} harness did not succeed: {status}")
        return {"output": result.get("output"), "status": status}

    return dispatch


async def run_team_harness(
    manifest: OHMManifest,
    harness: _Harness,
    *,
    sub_harnesses: dict[str, dict[str, Any]] | None = None,
    gate_decisions: dict[str, str] | None = None,
    completed: dict[str, Any] | None = None,
) -> TeamRunResult:
    """Run a Team Harness member DAG, dispatching each member as a real harness execution.

    ``completed`` (members that already ran in a prior drive) is passed through so a resume past a
    human gate does not re-dispatch already-finished members (their side effects fire once)."""
    dispatch = make_harness_dispatch(harness, sub_harnesses or {})
    return await run_team(manifest, dispatch, gate_decisions=gate_decisions, completed=completed)
