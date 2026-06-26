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

from oraclous_ohm.dag import strongly_connected_components
from oraclous_ohm.errors import OHMError
from oraclous_ohm.import_._flags import FlagSeverity, ImportFlag
from oraclous_ohm.import_.handoff import HandoffSpec
from oraclous_ohm.import_.schedules import ScheduledJob
from oraclous_ohm.manifest import (
    OHMLoop,
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

    # ADR-043 #552: isolate each GENUINE loop as a Tarjan strongly-connected component instead of
    # demoting the WHOLE handoff graph to a routing string at the first cycle (the pre-ADR-043
    # behaviour — a single back-edge among N agents flipped the entire team to depends_on=[] / run-
    # once). Build the combined directed graph — a handoff X->Y is an edge X->Y; a structural
    # depends_on (D in r.depends_on) is an edge D->r — and compute its SCCs. An SCC of >=2 members
    # (or a single node with a self-edge) is a real loop the conductor runs as a bounded coordinator
    # seam; every other node is the acyclic skeleton that still runs on run_team.
    graph: dict[str, set[str]] = {r: set() for r in roles}
    for frm, to in edges:
        graph[frm].add(to)
    for r in roles:
        for dep in by_role[r].depends_on:
            if dep in roles:
                graph[dep].add(r)
    sccs = strongly_connected_components(graph)
    scc_of = {role: i for i, scc in enumerate(sccs) for role in scc}
    loop_sccs = [
        scc for scc in sccs if len(scc) >= 2 or (len(scc) == 1 and scc[0] in graph[scc[0]])
    ]
    loop_roles = {role for scc in loop_sccs for role in scc}

    # an intra-loop structural depends_on would make the run_team skeleton cyclic — the coordinator
    # routes within the loop, so strip it (the loop seam carries it).
    for r in loop_roles:
        by_role[r].depends_on = [d for d in by_role[r].depends_on if scc_of.get(d) != scc_of[r]]

    # inter-SCC handoff edges become depends_on (the acyclic skeleton); intra-loop handoff edges are
    # carried by the loop seam, not depends_on (else the member DAG would be cyclic).
    inter_scc_edges = [(frm, to) for frm, to in edges if scc_of[frm] != scc_of[to]]
    for frm, to in inter_scc_edges:
        if frm not in by_role[to].depends_on:
            by_role[to].depends_on.append(frm)

    # each genuine loop becomes an OHMOrchestration loop seam, preserving each member's next_task so
    # the bounded coordinator (#552 step 2) re-dispatches the next member with a concrete objective.
    loops = [
        OHMLoop(
            members=scc,
            routing={
                role: handoffs[role].next_task
                for role in scc
                if role in handoffs and handoffs[role].next_task
            },
        )
        for scc in loop_sccs
    ]
    cyclic = bool(loops)
    # the conductor always carries an orchestration brief — its ``loops`` is the (possibly empty)
    # set of isolated coordinator seams; a purely acyclic team has loops=[] and runs on run_team.
    orchestration = orchestration or OHMOrchestration()
    orchestration.loops = loops
    if loops:
        flag(
            "F-LOOP-SCC",
            "info",
            f"{len(loops)} loop seam(s) isolated as coordinator seams ({len(loop_roles)} members); "
            "the acyclic remainder runs on run_team",
        )
    if inter_scc_edges:
        flag(
            "F-HANDOFF-EDGES", "info", f"{len(inter_scc_edges)} handoff depends_on edge(s) derived"
        )

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
