"""#594 (ADR-047 decision 2) — build the Harness Compiler AS an OHM v1.1 Team Harness.

Four members in a LINEAR ACYCLIC chain — planner → capability-surveyor → manifest-drafter →
reviewer (four sequential ``execution_stages()``). NO team-level loop / no engine done-check: the
repair loop is the REVIEWER's own in-harness tool-use loop (CTO decision A) — its single dispatch
iterates validate→fix→validate via the ``manifest-validate`` tool (#593 ``would_block``), bounded by
its iteration cap + the #585 pool. The drafter depends on BOTH the surveyor (the tool catalog) and
the planner (the DAG sketch) so it sees both upstream outputs; the chain stays acyclic.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from oraclous_ohm.compiler.prompts import (
    DRAFTER_PROMPT,
    PLANNER_PROMPT,
    REVIEWER_PROMPT,
    SURVEYOR_PROMPT,
)
from oraclous_ohm.import_.mapping import build_subharness
from oraclous_ohm.manifest import (
    OHMBudget,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMRuntime,
)
from oraclous_ohm.seeds import seed_policy_template, seed_reference_topologies

#: the reviewer's validate capability — the registered ``manifest-validate`` connector (slice-1).
_VALIDATE_TOOL = "manifest-validate"


def _planner_topology_subgoal(objective: str) -> str:
    """#596: the planner COMPOSES FROM the seed reference topology shapes (ADR-047 §5, DoD item 3) —
    their names + shapes are seeded into its sub-goal so it ADAPTS the closest one, never a frozen
    pipeline. The prose objective leads; the reference shapes follow as composables."""
    shapes = [{"name": t.name, "shape": t.description} for t in seed_reference_topologies()]
    guidance = (
        "Reference team shapes you may COMPOSE FROM (adapt the closest, never copy verbatim): "
        f"{json.dumps(shapes)}"
    )
    return f"{objective}\n\n{guidance}" if objective else guidance


def _drafter_governance_subgoal() -> str:
    """#596: the drafter's sub-goal seeds the GOVERNED-BY-DEFAULT policy template — it must emit the
    seed ``governance`` (a KNOWN policy_set_ref + redact_patterns) + the 3-layer ``budget`` VERBATIM
    on the compiled team, so a fresh org's compiled team ships governed (ADR-047 §5)."""
    p = seed_policy_template()
    seed = {
        "governance": p.governance.model_dump(mode="json", exclude_none=True),
        "budget": p.budget.model_dump(mode="json", exclude_none=True),
    }
    return (
        "GOVERNED-BY-DEFAULT: emit this seed policy VERBATIM as the team's `governance` and "
        f"`budget` (do not invent values): {json.dumps(seed)}"
    )


#: the bounded in-harness repair loop (CTO decision A / decision-3): the reviewer fixes a blocked
#: draft and re-validates at most ``_REPAIR_ATTEMPTS`` times (default 2 / max 3). Each attempt is
#: one ``manifest-validate`` tool call, so the loop is HARD-bounded by capping the reviewer member's
#: ``max_tool_calls`` at ``_REPAIR_ATTEMPTS + 1`` (the initial validate + N fixes) — the harness
#: halts the loop at the cap, independent of whether the model honours the prompt. After the cap it
#: fails closed with a gap report (no team JSON).
_REPAIR_ATTEMPTS = 2
_REVIEWER_VALIDATE_CALLS = _REPAIR_ATTEMPTS + 1


def build_compiler_team(
    owner_organization_id: uuid.UUID,
    *,
    objective: str = "",
    catalog: list[Any] | None = None,
    name: str = "harness-compiler",
) -> tuple[OHMManifest, dict[str, dict]]:
    """Return the compiler Team Harness manifest + its four member sub-harnesses (ready to POST to
    ``/v1/engine/team-runs``). The model is bound by the caller (BYOM).

    Slice-1 seeds the run deterministically by BAKING two values into the member sub-goals at build
    time (a member's sub-goal renders as its harness ``Objective:`` line — team_run._render_input —
    so no engine wiring is needed): the prose ``objective`` becomes the PLANNER's sub-goal, and the
    surveyed ``catalog`` (the tool ceiling) becomes the SURVEYOR's. The reviewer ``depends_on`` BOTH
    the drafter (the draft to validate) and the surveyor (the catalog to diff against); the chain
    stays acyclic (planner→surveyor→drafter→reviewer). A live survey connector is a fast-follow.
    """
    surveyor_goal = (
        f"The surveyed capability catalog (the ONLY tools a member may use): {json.dumps(catalog)}"
        if catalog
        else None
    )
    members = [
        OHMMember(
            role="planner",
            kind="agent",
            manifest_ref="org:compiler/planner@1",
            tools=[],
            # the prose objective + the seed reference topology shapes to compose from (#596 DoD 3).
            # NOTE: this rides the static sub-goal (the harness Objective: line); an inbound #577
            # objective_slice would shadow it, but the planner is the entrypoint (no inbound
            # producer), so that cannot happen here.
            subgoal=_planner_topology_subgoal(objective),
        ),
        OHMMember(
            role="capability-surveyor",
            kind="agent",
            manifest_ref="org:compiler/surveyor@1",
            tools=[],
            depends_on=["planner"],
            subgoal=surveyor_goal,  # the seeded catalog IS the surveyor's task (deterministic)
        ),
        OHMMember(
            role="manifest-drafter",
            kind="agent",
            manifest_ref="org:compiler/drafter@1",
            tools=[],
            depends_on=["capability-surveyor", "planner"],
            # #596: emit the seed governance + budget. NOTE: this rides the static sub-goal; the
            # compiler's surveyor/planner emit NO ## Handoff objective_slice (#577), so nothing
            # shadows it — but if handoff wiring is ever added upstream, guard this governance seed.
            subgoal=_drafter_governance_subgoal(),
        ),
        OHMMember(
            role="reviewer",
            kind="agent",
            manifest_ref="org:compiler/reviewer@1",
            tools=[_VALIDATE_TOOL],  # the in-harness repair loop calls validate via this capability
            # sees the draft (drafter) AND the catalog (surveyor) — both needed to diff AND to fix
            depends_on=["manifest-drafter", "capability-surveyor"],
            # HARD bound on the validate→fix→validate loop: initial validate + at most N fixes
            max_tool_calls=_REVIEWER_VALIDATE_CALLS,
        ),
    ]
    manifest = OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(
            id=uuid.uuid4(),
            name=name,
            owner_organization_id=owner_organization_id,
            kind="team",
        ),
        members=members,
        runtime=OHMRuntime(entrypoint="planner"),
        # the 3-layer budget: a team pool + a per-member safety cap (each <= the pool).
        budget=OHMBudget(max_tokens_total=200_000, max_sub_runs=20, max_tokens_per_member=60_000),
    )

    def _sub(role: str, body: str, tools: list[str]) -> dict:
        return build_subharness(
            role, owner_organization_id=owner_organization_id, body=body, tools=tools
        ).model_dump(mode="json")

    sub_harnesses = {
        "planner": _sub("planner", PLANNER_PROMPT, []),
        "capability-surveyor": _sub("capability-surveyor", SURVEYOR_PROMPT, []),
        "manifest-drafter": _sub("manifest-drafter", DRAFTER_PROMPT, []),
        "reviewer": _sub("reviewer", REVIEWER_PROMPT, [_VALIDATE_TOOL]),
    }
    return manifest, sub_harnesses
