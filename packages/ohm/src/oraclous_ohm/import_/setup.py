"""The import front door + O8 dry-run report (#409; ADR-034 §1/§7).

``import_setup(path)`` is the single product action ADR-034 §1 promised: it discovers an agent
setup (``.claude/agents`` + ``.claude/skills`` + ``teams/*/charter.md`` + ``cron.yaml``, or a single
orchestrator skill), runs the whole importer (#405-#408), and emits an ``ImportReport`` — the parsed
team, the generated DAG, which skills resolved vs failed, the per-member ceilings, and every flag by
severity — **before any live run**. Read-only: no dispatch, no schedule armed, no side effect. Pure.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.import_._flags import ImportFlag
from oraclous_ohm.import_.assemble import TeamAssembly, assemble_team
from oraclous_ohm.import_.charter import CharterTeam, parse_charter
from oraclous_ohm.import_.handoff import HandoffSpec, parse_handoff
from oraclous_ohm.import_.mapping import map_agent_to_member, slugify
from oraclous_ohm.import_.orchestrator import adapt_orchestrator_skill
from oraclous_ohm.import_.parse import discover_agents, parse_agent_file
from oraclous_ohm.import_.schedules import parse_cron
from oraclous_ohm.import_.skills import resolve_skill
from oraclous_ohm.manifest import OHMManifest, OHMMember


class ImportReport(BaseModel):
    """The O8 dry-run report — what an import produced, before any live run."""

    model_config = ConfigDict(extra="ignore")

    team_name: str
    shape: str  # "agent-team" | "orchestrator" | "none"
    member_count: int = 0
    human_gate_count: int = 0
    stages: list[list[str]] = Field(default_factory=list)  # the worker DAG ([] if no members)
    cyclic_routing: bool = False
    schedules: dict[str, str] = Field(default_factory=dict)  # role -> cron
    resolved_skills: int = 0
    unresolved_skills: int = 0
    blocking: list[str] = Field(default_factory=list)  # blocking-flag messages
    confirm_count: int = 0
    info_count: int = 0

    @property
    def would_block(self) -> bool:
        return bool(self.blocking)


class ImportResult(BaseModel):
    """The assembled team (None if nothing importable) + its dry-run report + all flags."""

    model_config = ConfigDict(extra="ignore")

    manifest: OHMManifest | None = None
    report: ImportReport
    flags: list[ImportFlag] = Field(default_factory=list)
    # role -> the generated single-agent sub-harness OHM (as a plain dict), ready to pass straight
    # into the team-run API's `sub_harnesses`. Without this the imported team LOADS but cannot RUN
    # (its members' manifest_refs resolve to nothing registered) — the import->run seam (ADR-035).
    sub_harnesses: dict[str, dict] = Field(default_factory=dict)


def _is_orchestrator_dir(root: Path) -> bool:
    return (root / "SKILL.md").is_file() and (root / "modules").is_dir()


def _build_report(
    name: str, shape: str, assembly: TeamAssembly | None, flags: list[ImportFlag]
) -> ImportReport:
    stages: list[list[str]] = []
    cyclic = False
    schedules: dict[str, str] = {}
    members: list[OHMMember] = []
    if assembly is not None:
        members = list(assembly.manifest.members)
        cyclic = assembly.cyclic_routing
        schedules = {m.role: m.schedule for m in members if m.schedule}
        try:
            stages = assembly.manifest.execution_stages()
        except Exception:  # a malformed DAG is already flagged blocking by the assembler
            stages = []
    by_sev: dict[str, list[ImportFlag]] = {"blocking": [], "confirm": [], "info": []}
    for f in flags:
        by_sev[f.severity].append(f)
    return ImportReport(
        team_name=name,
        shape=shape,
        member_count=len(members),
        human_gate_count=sum(1 for m in members if m.kind == "human"),
        stages=stages,
        cyclic_routing=cyclic,
        schedules=schedules,
        resolved_skills=sum(1 for f in flags if f.code == "F-SKILL-RESOLVED"),
        unresolved_skills=sum(1 for f in flags if f.code == "F-SKILL-MISSING"),
        blocking=[f"{f.code}: {f.message}" for f in by_sev["blocking"]],
        confirm_count=len(by_sev["confirm"]),
        info_count=len(by_sev["info"]),
    )


def _fold_charter_pipeline(
    charters: list[CharterTeam], by_role: dict[str, OHMMember]
) -> tuple[int, int]:
    """Chain charter teams + their gates into a pipeline (team/gate N depends_on N-1).

    Book-shaped setups carry no ``## Handoff`` edges — their structure is the charters; each
    team's agent members + the gates it owns become an ordered stage; stage N depends_on all of N-1.
    Roster entries that aren't mapped members (skills, unbuilt agents) are skipped. Acyclic by team
    order; ``assemble_team`` re-validates. Returns (stage_count, edge_count).
    """
    member_roles = set(by_role)
    stages: list[list[str]] = []
    seen_gates: set[str] = set()
    for charter in sorted(charters, key=lambda c: c.team_num if c.team_num is not None else 999):
        team_roles = [
            r for r in (slugify(e.agent_name) for e in charter.roster) if r in member_roles
        ]
        if team_roles:
            stages.append(team_roles)
        for gate in charter.hard_gates:
            grole = f"gate-{gate.gate_id.lower()}"
            if grole in member_roles and grole not in seen_gates:
                seen_gates.add(grole)
                stages.append([grole])
    edges = 0
    for i in range(1, len(stages)):
        for role in stages[i]:
            for prev in stages[i - 1]:
                if prev != role and prev not in by_role[role].depends_on:
                    by_role[role].depends_on.append(prev)
                    edges += 1
    return len(stages), edges


def import_setup(
    path: str | Path, *, owner_organization_id: uuid.UUID, name: str | None = None
) -> ImportResult:
    """Discover a setup, run the importer, and return the team + O8 dry-run report."""
    root = Path(path)
    team_name = name or root.name
    flags: list[ImportFlag] = []
    members: list[OHMMember] = []
    handoffs: dict[str, HandoffSpec] = {}
    sub_harnesses: dict[str, dict] = {}  # role -> generated sub-harness OHM (the runnable bodies)
    orchestration = None
    shape = "none"

    agents_dir = root / ".claude" / "agents"
    skills_dir = root / ".claude" / "skills"
    skills_root = skills_dir if skills_dir.is_dir() else None

    if agents_dir.is_dir():
        shape = "agent-team"
        for agent_file in discover_agents(agents_dir):
            agent = parse_agent_file(agent_file)
            mapping = map_agent_to_member(
                agent, owner_organization_id=owner_organization_id, skills_root=skills_root
            )
            members.append(mapping.member)
            flags.extend(mapping.flags)
            handoffs[mapping.member.role] = parse_handoff(agent.body)
            if mapping.sub_harness is not None:  # keep the runnable body, don't discard it (G-B)
                sub_harnesses[mapping.member.role] = mapping.sub_harness.model_dump(mode="json")
        by_role = {m.role: m for m in members}
        charters = [parse_charter(cf) for cf in sorted(root.glob("teams/*/charter.md"))]
        for charter in charters:
            flags.extend(charter.flags)
            for gate in charter.hard_gates:
                role = f"gate-{gate.gate_id.lower()}"
                if role not in by_role:
                    member = OHMMember(
                        role=role, kind="human", human_role="operator", subgoal=gate.description
                    )
                    members.append(member)
                    by_role[role] = member
                    flags.append(
                        ImportFlag(
                            code="F-CHARTER-GATE-MEMBER",
                            severity="info",
                            member_role=role,
                            message=f"hard gate {gate.gate_id} -> human member",
                        )
                    )
        if charters:
            stage_count, edge_count = _fold_charter_pipeline(charters, by_role)
            if edge_count:
                flags.append(
                    ImportFlag(
                        code="F-CHARTER-PIPELINE",
                        severity="info",
                        member_role="",
                        message=f"charter team order -> {edge_count} edges, {stage_count} stages",
                    )
                )
    elif _is_orchestrator_dir(root):
        shape = "orchestrator"
        resolved = resolve_skill(root.name, root.parent)
        plan = adapt_orchestrator_skill(
            resolved, owner_organization_id=owner_organization_id, skills_root=root.parent
        )
        members = plan.members
        orchestration = plan.orchestration
        flags.extend(plan.flags)
        for role, sub in plan.sub_harnesses.items():  # keep the orchestrator bodies too (G-B)
            sub_harnesses[role] = sub.model_dump(mode="json")
    else:
        flags.append(
            ImportFlag(
                code="F-NO-SETUP",
                severity="blocking",
                member_role="",
                message=f"no importable setup at {root} (no .claude/agents or orchestrator skill)",
            )
        )
        return ImportResult(
            manifest=None, report=_build_report(team_name, shape, None, flags), flags=flags
        )

    cron_files = sorted(root.glob("**/cron.yaml"))
    schedules = parse_cron(cron_files[0]) if cron_files else []

    assembly = assemble_team(
        team_name,
        members,
        owner_organization_id=owner_organization_id,
        handoffs=handoffs,
        schedules=schedules,
        orchestration=orchestration,
    )
    flags.extend(assembly.flags)
    return ImportResult(
        manifest=assembly.manifest,
        report=_build_report(team_name, shape, assembly, flags),
        flags=flags,
        sub_harnesses=sub_harnesses,
    )


def render_report(report: ImportReport) -> str:
    """Render an ImportReport as a human-readable dry-run summary (the O8 surface)."""
    lines = [
        f"Import dry-run: {report.team_name} ({report.shape})",
        f"  members: {report.member_count} ({report.human_gate_count} human gates)",
        f"  DAG stages: {len(report.stages)}"
        + (" — cyclic/standing team (routing, not pipeline)" if report.cyclic_routing else ""),
        f"  schedules: {len(report.schedules)}",
        f"  skills: {report.resolved_skills} resolved, {report.unresolved_skills} unresolved",
        f"  flags: {len(report.blocking)} blocking, {report.confirm_count} confirm, "
        f"{report.info_count} info",
        f"  GO: {'BLOCKED' if report.would_block else 'ready (pending review of confirm flags)'}",
    ]
    lines.extend(f"    BLOCK {b}" for b in report.blocking)
    return "\n".join(lines)
