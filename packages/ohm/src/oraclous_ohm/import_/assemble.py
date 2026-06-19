"""Assemble parsed sources into one OHM v1.1 Team Harness (ADR-034 §6 — DAG-from-source).

The convergence of the importer: given the member set (from #405 agents / #406 charter human-gates /
#407 orchestrator), this derives the inter-member DAG and folds it into one ``OHMManifest`` with
``metadata.kind == "team"``. Two derivation sources beyond the structural depends_on:

* **handoffs** (``## Handoff`` Next-agent) — ``X -> Y`` edges (Y depends_on X), applied only if they
  keep the member DAG acyclic; a cyclic handoff graph means a *standing/scheduled* team (not a
  pipeline), so the edges are demoted to routing-hints on ``orchestration.style`` + a flag.
* **schedules** (cron.yaml) — attached to each member's ``schedule`` by matching ``agent`` to role.

Pure; fail-closed; flag-not-guess. The discovery producing these inputs + the dry-run is #409.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.dag import topological_stages
from oraclous_ohm.errors import OHMDagError, OHMError
from oraclous_ohm.import_._flags import FlagSeverity, ImportFlag
from oraclous_ohm.import_.handoff import HandoffSpec
from oraclous_ohm.import_.schedules import ScheduledJob
from oraclous_ohm.manifest import (
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRuntime,
)
from oraclous_ohm.parse import load_ohm


class TeamAssembly(BaseModel):
    """The assembled Team Harness + the import flags + whether handoffs were demoted to routing."""

    model_config = ConfigDict(extra="ignore")

    manifest: OHMManifest
    flags: list[ImportFlag] = Field(default_factory=list)
    cyclic_routing: bool = False


def assemble_team(
    name: str,
    members: list[OHMMember],
    *,
    owner_organization_id: uuid.UUID,
    handoffs: dict[str, HandoffSpec] | None = None,
    schedules: list[ScheduledJob] | None = None,
    orchestration: OHMOrchestration | None = None,
    entrypoint: str | None = None,
) -> TeamAssembly:
    """Fold members + handoff edges + schedules into one OHM v1.1 Team Harness (ADR-034 §6)."""
    handoffs = handoffs or {}
    schedules = schedules or []
    members = [m.model_copy(deep=True) for m in members]  # never mutate the caller's members
    by_role = {m.role: m for m in members}
    roles = set(by_role)

    flags: list[ImportFlag] = []

    def flag(code: str, severity: FlagSeverity, message: str, role: str = "") -> None:
        flags.append(ImportFlag(code=code, severity=severity, member_role=role, message=message))

    # 1. attach cron schedules (job.agent -> member.role)
    for job in schedules:
        target = by_role.get(job.agent)
        if target is None:
            flag(
                "F-SCHEDULE-NOMATCH",
                "confirm",
                f"cron job {job.id!r} targets unknown agent {job.agent!r}",
            )
            continue
        target.schedule = job.cron
        flag("F-SCHEDULE-ATTACHED", "info", f"schedule {job.cron!r} ({job.id}) attached", job.agent)

    # 2. candidate handoff edges: X hands off to Y  =>  Y depends_on X
    edges = [
        (frm, to)
        for frm, spec in handoffs.items()
        if frm in roles
        for to in spec.next_agents
        if to in roles and to != frm
    ]

    # would applying them keep the DAG acyclic? (test on a trial copy)
    trial = {r: list(by_role[r].depends_on) for r in roles}
    for frm, to in edges:
        if frm not in trial[to]:
            trial[to].append(frm)
    trial_members = [
        OHMMember(
            role=r,
            kind=by_role[r].kind,
            manifest_ref=by_role[r].manifest_ref,
            human_role=by_role[r].human_role,
            depends_on=trial[r],
        )
        for r in roles
    ]
    cyclic = False
    try:
        topological_stages(trial_members)
    except OHMDagError:
        cyclic = True

    if cyclic:
        # standing/scheduled team — do NOT force a DAG; record handoffs as routing hints
        routing = "; ".join(f"{frm}->{to}" for frm, to in edges)
        orchestration = orchestration or OHMOrchestration()
        orchestration.style = (orchestration.style + f"\nHandoff routing: {routing}").strip()
        flag(
            "F-CYCLIC-ROUTING",
            "confirm",
            f"handoff graph is cyclic ({len(edges)} edges) — a standing/scheduled team; "
            "handoffs recorded as routing, not depends_on",
        )
    else:
        for frm, to in edges:
            if frm not in by_role[to].depends_on:
                by_role[to].depends_on.append(frm)
        if edges:
            flag("F-HANDOFF-EDGES", "info", f"{len(edges)} handoff depends_on edge(s) derived")

    # 3. conditional dispatch
    for frm, spec in handoffs.items():
        if spec.conditional and frm in roles:
            flag("F-HANDOFF-CONDITIONAL", "confirm", f"handoff from {frm!r} is conditional", frm)

    # 4. entrypoint: caller-supplied, else a root member (no depends_on), else the first member
    if entrypoint is None:
        roots = [m.role for m in members if not m.depends_on]
        entrypoint = roots[0] if roots else (members[0].role if members else "")

    manifest = OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(
            id=uuid.uuid4(), name=name, owner_organization_id=owner_organization_id, kind="team"
        ),
        members=members,
        orchestration=orchestration,
        runtime=OHMRuntime(entrypoint=entrypoint),
    )

    # the assembled team must load through the real loader (acyclic members, resolvable entrypoint)
    try:
        load_ohm(manifest.model_dump(mode="json"))
    except OHMError as exc:
        flag("F-ASSEMBLY-INVALID", "blocking", f"assembled team failed to load: {exc}")

    return TeamAssembly(manifest=manifest, flags=flags, cyclic_routing=cyclic)
