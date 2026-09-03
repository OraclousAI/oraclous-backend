"""#594 (ADR-047 decision 2) — build the Harness Compiler AS an OHM v1.1 Team Harness.

Three members in a LINEAR ACYCLIC chain — planner → manifest-drafter → reviewer (three sequential
``execution_stages()``). NO team-level loop / no engine done-check: the repair loop is the
REVIEWER's own in-harness tool-use loop (CTO decision A) — its single dispatch iterates
validate→fix→validate via the ``manifest-validate`` tool (#593 ``would_block``), bounded by its
iteration cap + the #585 pool.

#709 deleted the capability-surveyor step: its only job was retyping the surveyed catalog as its
own output, and nothing read that output any more — the drafter gets the described catalog baked
directly into its own sub-goal (#713 ``_drafter_governance_subgoal``/``_catalog_menu``), and the
reviewer's ``manifest-validate`` (``ManifestValidateConnector``) reads the org's live tool list
directly from the registry, never from anything the surveyor produced.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from oraclous_ohm.compiler.prompts import DRAFTER_PROMPT, PLANNER_PROMPT, REVIEWER_PROMPT
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


#: #713: how much of a tool's registry description rides the drafter's sub-goal. Descriptions are
#: capped at 500 chars at MCP import, and a busy org offers tens of tools, so the untrimmed union
#: would be several thousand characters of prompt on every compile. Observed on the deployed stack:
#: the useful part — what the tool is FOR, and the caveat that decides between two similar tools —
#: lives in the first couple of sentences; past that a description is a method list. 300 keeps
#: ``add_issue_comment``'s "use this with pull requests as well" caveat whole.
_DESCRIPTION_CHARS = 300


def _catalog_menu(catalog_descriptions: list[Any] | None) -> str:
    """#713: render the surveyed tools as ``[{name, description}]`` for the DRAFTER.

    An entry with no description renders as its name alone — a seed-inventory tool has no
    descriptor row, and inventing text for it would be worse than silence because the drafter would
    act on it. Descriptions are trimmed to ``_DESCRIPTION_CHARS`` so a large catalog cannot inflate
    every compile's prompt without bound."""
    menu: list[dict[str, str]] = []
    for entry in catalog_descriptions or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        item = {"name": str(entry["name"])}
        description = entry.get("description")
        if isinstance(description, str) and description.strip():
            item["description"] = description.strip()[:_DESCRIPTION_CHARS]
        menu.append(item)
    return json.dumps(menu)


def _drafter_governance_subgoal(catalog_descriptions: list[Any] | None = None) -> str:
    """#596: the drafter's sub-goal seeds the GOVERNED-BY-DEFAULT policy template — it must emit the
    seed ``governance`` (a KNOWN policy_set_ref + redact_patterns) + the 3-layer ``budget`` VERBATIM
    on the compiled team, so a fresh org's compiled team ships governed (ADR-047 §5).

    #713 adds the described tool menu here, and HERE specifically. Before #709 the drafter read a
    now-deleted capability-surveyor member's OUTPUT, which echoed ``{name, ref}`` only — so a
    description hung on that step would have had to survive a model retyping tens of them, the
    same relay #705 already found unreliable (one name dropped while re-typing a 72-entry list
    blocked an entire compile). Baking the menu into the drafter's own sub-goal is deterministic:
    the descriptions reach the model that picks the tools, unretyped."""
    p = seed_policy_template()
    seed = {
        "governance": p.governance.model_dump(mode="json", exclude_none=True),
        "budget": p.budget.model_dump(mode="json", exclude_none=True),
    }
    parts = [
        "GOVERNED-BY-DEFAULT: emit this seed policy VERBATIM as the team's `governance` and "
        f"`budget` (do not invent values): {json.dumps(seed)}"
    ]
    if catalog_descriptions:
        parts.append(
            "WHAT EACH SURVEYED TOOL DOES — read this before assigning any tool, and give a member "
            "only a tool whose description fits that member's sub-goal. A tool that cannot do the "
            "job is worse than no tool: the member will call it, get nothing, and answer anyway. "
            f"{_catalog_menu(catalog_descriptions)}"
        )
    return "\n\n".join(parts)


#: the bounded in-harness repair loop (CTO decision A / decision-3): the reviewer fixes a blocked
#: draft and re-validates at most ``_REPAIR_ATTEMPTS`` times (default 2 / max 3). Each attempt is
#: one ``manifest-validate`` tool call.
_REPAIR_ATTEMPTS = 2
#: #596: the HARD ``max_tool_calls`` cap is the repair budget (initial validate + N fixes) PLUS
#: explicit slack for a WEAK BYOM model (e.g. gpt-4o-mini) benignly RE-VALIDATING a draft that
#: already passed ``would_block=False`` — observed on the deployed stack: the model re-checks a
#: clean team and, with only ``N+1`` slots, hits the cap and the compile degrades on that caution
#: alone. The slack keeps the loop HARD-bounded (no runaway) while a clean compile finishes;
#: the bound still fail-closes a persistently-blocked draft. (Cap raised from N+1=3; CTO-flagged.)
_REVIEWER_OVERCHECK_SLACK = 5
_REVIEWER_VALIDATE_CALLS = _REPAIR_ATTEMPTS + 1 + _REVIEWER_OVERCHECK_SLACK


def build_compiler_team(
    owner_organization_id: uuid.UUID,
    *,
    objective: str = "",
    catalog_descriptions: list[Any] | None = None,
    name: str = "harness-compiler",
) -> tuple[OHMManifest, dict[str, dict]]:
    """Return the compiler Team Harness manifest + its three member sub-harnesses (ready to POST to
    ``/v1/engine/team-runs``). The model is bound by the caller (BYOM).

    Slice-1 seeds the run deterministically by BAKING the prose ``objective`` into the PLANNER's
    sub-goal at build time (a member's sub-goal renders as its harness ``Objective:`` line —
    team_run._render_input — so no engine wiring is needed). The chain is
    planner → manifest-drafter → reviewer, acyclic.

    ``catalog_descriptions`` (#713) is the surveyed catalog as ``[{name, description}]`` and is
    baked into the DRAFTER's sub-goal — see ``_drafter_governance_subgoal``. #709 deleted the
    capability-surveyor step that used to carry the bare-name catalog: nothing read its retyped
    output any more, since the drafter reads the described catalog directly and the reviewer's
    ``manifest-validate`` reads the org's live registry directly. Omit ``catalog_descriptions``
    (the unit path, or a registry outage degrading to the seed inventory) and the drafter's
    sub-goal is byte-identical to the no-catalog case.
    """
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
            role="manifest-drafter",
            kind="agent",
            manifest_ref="org:compiler/drafter@1",
            tools=[],
            depends_on=["planner"],
            # #596: emit the seed governance + budget. NOTE: this rides the static sub-goal; the
            # compiler's planner emits NO ## Handoff objective_slice (#577), so nothing shadows it
            # — but if handoff wiring is ever added upstream, guard this governance seed.
            subgoal=_drafter_governance_subgoal(catalog_descriptions),
        ),
        OHMMember(
            role="reviewer",
            kind="agent",
            manifest_ref="org:compiler/reviewer@1",
            tools=[_VALIDATE_TOOL],  # the in-harness repair loop calls validate via this capability
            depends_on=["manifest-drafter"],
            # HARD bound on the validate→fix→validate loop: initial validate + at most N fixes
            max_tool_calls=_REVIEWER_VALIDATE_CALLS,
            # #596: DEGRADE (not escalate) on exhausting that bound — a richer plan can make the
            # reviewer over-validate past the cap; a compile must finish with a best-effort partial
            # (#587 degrade), NEVER fail the whole compile because the model double-checked.
            on_exhaustion="degrade",
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
        "manifest-drafter": _sub("manifest-drafter", DRAFTER_PROMPT, []),
        "reviewer": _sub("reviewer", REVIEWER_PROMPT, [_VALIDATE_TOOL]),
    }
    return manifest, sub_harnesses
