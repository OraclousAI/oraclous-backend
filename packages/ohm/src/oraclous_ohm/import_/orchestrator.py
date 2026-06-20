"""Adapt a single-skill ORCHESTRATOR into an OHM v1.1 ``orchestration`` + ``members[]`` DAG (§5).

ADR-034 §5. The thing #406's ``classify_skill`` flags-but-never-inlines (``kind=="orchestrator"``)
becomes the team backbone here. A ``modules/<wave>/*.md`` layout maps to waves: one member per
module (each a distinct brief), wave order from the global ``NN-`` prefix, ``depends_on`` = all of
the previous wave (fan-in barrier). Orchestrator-internal barrier scripts (setup/merge) are NOT
subagents, so they are not emitted. Pure; fail-closed; flag-not-guess. Each member also gets a
loadable (tool-less) sub-harness carrying its module brief; merging with ``.claude/agents`` is #408.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_._flags import FlagSeverity, ImportFlag
from oraclous_ohm.import_.mapping import build_subharness, slugify
from oraclous_ohm.import_.skills import ResolvedSkill, resolve_skill
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMOrchestration, OHMTermination

_NUM_PREFIX = re.compile(r"^(\d+)[-_]?(.*)$")
_MODE_RE = re.compile(r"(--[a-z][a-z0-9-]+)")
_FANOUT_CANDIDATE_WIDTH = 4  # a wave this wide could collapse to a fan_out member (#408 refinement)


class OrchestratorPlan(BaseModel):
    """Result of adapting one orchestrator skill: the orchestration block + the members[] DAG."""

    model_config = ConfigDict(extra="ignore")

    orchestration: OHMOrchestration
    members: list[OHMMember] = Field(default_factory=list)
    sub_harnesses: dict[str, OHMManifest] = Field(
        default_factory=dict
    )  # role -> module sub-harness
    flags: list[ImportFlag] = Field(default_factory=list)
    conditional_modes: list[str] = Field(default_factory=list)  # surfaced, not modeled as members


def _module_number(filename: str) -> int | None:
    m = _NUM_PREFIX.match(Path(filename).stem)
    return int(m.group(1)) if m else None


def _role_from_module(filename: str) -> str:
    m = _NUM_PREFIX.match(Path(filename).stem)
    return slugify(m.group(2) if m and m.group(2) else Path(filename).stem)


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""


def _section(body: str, keywords: tuple[str, ...]) -> str:
    """Body under the first ``## `` heading matching any keyword (until the next heading)."""
    out: list[str] = []
    capturing = False
    for line in body.splitlines():
        if line.startswith("## "):
            capturing = any(k in line[3:].strip().lower() for k in keywords)
        elif line.startswith("# "):
            capturing = False
        elif capturing:
            out.append(line)
    return "\n".join(out).strip()


def adapt_orchestrator_skill(
    resolved: ResolvedSkill, *, owner_organization_id: uuid.UUID, skills_root: str | Path
) -> OrchestratorPlan:
    """Adapt an orchestrator ``ResolvedSkill`` into an ``OrchestratorPlan`` (ADR-034 §5)."""
    if resolved.kind != "orchestrator":
        raise OHMImportError(f"skill {resolved.name!r} is a leaf; #407 adapts orchestrators only")

    flags: list[ImportFlag] = []

    def flag(code: str, severity: FlagSeverity, message: str, role: str = "") -> None:
        flags.append(ImportFlag(code=code, severity=severity, member_role=role, message=message))

    modes = sorted(set(_MODE_RE.findall(resolved.body)))
    for mode in modes:
        flag(f"F-MODE-{mode.lstrip('-').upper()}", "confirm", f"mode {mode} surfaced; not a member")

    orchestration = OHMOrchestration(
        medium=["blackboard"],
        style=_section(resolved.body, ("phase", "wave")) or resolved.description,
        success_criteria=_section(
            resolved.body, ("hard rule", "non-negotiable", "success", "criteria", "must")
        ),
        termination=OHMTermination(),
    )
    flag(
        "F-MEDIUM-INFERRED",
        "confirm",
        "coordination medium inferred 'blackboard' (disk-first persistence)",
    )
    flag(
        "F-TERMINATION-ABSENT",
        "info",
        "no termination bounds in skill prose; pooled budget is the ceiling",
    )

    modules_dir = Path(skills_root) / resolved.name / "modules"
    wave_dirs = (
        [d for d in modules_dir.iterdir() if d.is_dir() and any(d.glob("*.md"))]
        if modules_dir.is_dir()
        else []
    )
    if not wave_dirs:
        flag("F-ORCH-UNSTRUCTURED", "blocking", "no modules/<wave>/ layout; cannot derive members")
        return OrchestratorPlan(
            orchestration=orchestration, members=[], flags=flags, conditional_modes=modes
        )

    def wave_min(d: Path) -> int:
        nums = [n for f in d.glob("*.md") if (n := _module_number(f.name)) is not None]
        return min(nums) if nums else 10**9

    ordered = sorted(wave_dirs, key=lambda d: (wave_min(d), d.name))
    if any(wave_min(d) == 10**9 for d in ordered):
        flag(
            "F-ORCH-WAVE-ORDER",
            "confirm",
            "some modules lack a numeric prefix; wave order may be wrong",
        )

    members: list[OHMMember] = []
    sub_harnesses: dict[str, OHMManifest] = {}
    seen: set[str] = set()
    prev_roles: list[str] = []
    for d in ordered:
        wave_files = sorted(d.glob("*.md"), key=lambda f: (_module_number(f.name) or 0, f.name))
        wave_roles: list[str] = []
        for f in wave_files:
            role = _role_from_module(f.name)
            if not role:
                continue
            if role in seen:
                flag(
                    "F-ROLE-DUP",
                    "confirm",
                    f"duplicate member role {role!r} from {f.name}; suffixed",
                )
                role = f"{role}-{_module_number(f.name) or len(seen)}"
            seen.add(role)
            text = f.read_text(encoding="utf-8")
            members.append(
                OHMMember(
                    role=role,
                    kind="agent",
                    manifest_ref=f"org:{owner_organization_id}/{role}@1",
                    subgoal=_first_heading(text) or role,
                    depends_on=list(prev_roles),
                )
            )
            sub_harnesses[role] = build_subharness(
                role,
                owner_organization_id=owner_organization_id,
                body=text,
                source=f"{resolved.name}/modules/{d.name}/{f.name}",
            )
            wave_roles.append(role)
            flag(
                "F-MEMBER-SUBHARNESS",
                "info",
                f"member {role!r} sub-harness built from module {f.name} (tool-less)",
                role,
            )
        if len(wave_roles) >= _FANOUT_CANDIDATE_WIDTH:
            flag(
                "F-FANOUT-CANDIDATE",
                "info",
                f"wave {d.name!r} has {len(wave_roles)} parallel members; fan_out candidate",
            )
        prev_roles = wave_roles

    return OrchestratorPlan(
        orchestration=orchestration,
        members=members,
        sub_harnesses=sub_harnesses,
        flags=flags,
        conditional_modes=modes,
    )


def adapt_orchestrator_skill_by_name(
    name: str, skills_root: str | Path, *, owner_organization_id: uuid.UUID
) -> OrchestratorPlan:
    """Convenience: resolve ``name`` from ``skills_root`` (strict) then adapt it."""
    return adapt_orchestrator_skill(
        resolve_skill(name, skills_root),
        owner_organization_id=owner_organization_id,
        skills_root=skills_root,
    )
