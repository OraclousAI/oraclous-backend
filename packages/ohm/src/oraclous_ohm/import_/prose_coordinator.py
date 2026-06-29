"""#577 slice-2 — adapt a PROSE coordinator skill into an OHM v1.1 orchestration + members[] DAG.

ADR-034 §6 deferred the prose-showrunner shape (``book-studio``): subcommands + a numbered
``chapter <CH-NN>`` pipeline with ``∥`` parallel markers and ``GATE A/B/C`` human gates, and NO
``modules/<wave>/`` layout — so the §5 wave adapter (orchestrator.py) cannot consume it. This parses
that prose into agent members (sequence → ``depends_on``), same-stage ``∥`` siblings, and human
gate barriers (``human_role="author"``). ``BLOCK on CRITICAL`` is surfaced as a flag; the runtime
skip-guard is deferred. Pure; fail-closed; flag-not-guess. Only the ``chapter`` subcommand is parsed
(the load-bearing one for M1); the other subcommands are flagged as deferred.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from oraclous_ohm.import_._flags import FlagSeverity, ImportFlag
from oraclous_ohm.import_.mapping import build_subharness, slugify
from oraclous_ohm.import_.orchestrator import OrchestratorPlan
from oraclous_ohm.import_.skills import ResolvedSkill
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMOrchestration, OHMTermination

# the `chapter <CH-NN>` subcommand heading, then a fenced numbered list of steps
_CHAPTER_HEADING = re.compile(r"^###\s+`?chapter\b", re.IGNORECASE | re.MULTILINE)
_FENCE = re.compile(r"```.*?\n(.*?)```", re.DOTALL)
_STEP = re.compile(r"^\s*(\d+)\.\s+(.+)$")
_GATE = re.compile(r"GATE\s+([A-Z])")
_BLOCK = re.compile(r"BLOCK\s+on\s+(\w+)")
_NAME = re.compile(r"\s*([a-z][a-z0-9-]*)")
_ARROW = "→"  # the step's agent name(s) precede the output arrow
_PARALLEL = "∥"  # same-stage siblings


def _chapter_steps(body: str) -> list[tuple[int, str]]:
    """The (number, content) pairs of the ``chapter`` pipeline's numbered list (empty if absent)."""
    heading = _CHAPTER_HEADING.search(body)
    if heading is None:
        return []
    after = body[heading.end() :]
    fence = _FENCE.search(after)
    block = fence.group(1) if fence else after
    steps: list[tuple[int, str]] = []
    for line in block.splitlines():
        step = _STEP.match(line)
        if step:
            steps.append((int(step.group(1)), step.group(2).strip()))
    return steps


def has_prose_pipeline(body: str) -> bool:
    """True when a body declares a ``chapter`` prose pipeline (the no-``modules/`` case the wave
    adapter cannot consume)."""
    return bool(_CHAPTER_HEADING.search(body)) and bool(_chapter_steps(body))


def _parse_step(content: str) -> tuple[list[str], str | None, str | None, bool]:
    """Extract (agent names, gate letter, block keyword, optional?) from one step line."""
    optional = "(optional)" in content
    # names are before the output arrow; a numbered line WITHOUT an arrow is prose (e.g. "the author
    # decides …") or a bare gate line, NOT an agent step — so it mints no member (robustness guard).
    spec = content.split(_ARROW)[0] if _ARROW in content else ""
    names: list[str] = []
    for part in spec.split(_PARALLEL):
        match = _NAME.match(part.replace("(optional)", ""))
        if match and match.group(1) != "optional":
            names.append(match.group(1))
    gate = _GATE.search(content)
    block = _BLOCK.search(content)
    return names, (gate.group(1) if gate else None), (block.group(1) if block else None), optional


def adapt_prose_coordinator_skill(
    resolved: ResolvedSkill,
    *,
    owner_organization_id: uuid.UUID,
    skills_root: str | Path,  # noqa: ARG001 — symmetry with the wave adapter; prose is in-body
) -> OrchestratorPlan:
    """Adapt a prose-coordinator skill (a ``chapter`` pipeline) into an OrchestratorPlan."""
    flags: list[ImportFlag] = []

    def flag(code: str, severity: FlagSeverity, message: str, role: str = "") -> None:
        flags.append(ImportFlag(code=code, severity=severity, member_role=role, message=message))

    orchestration = OHMOrchestration(
        medium=["blackboard"],
        style=resolved.description or "book-studio chapter pipeline",
        success_criteria="",
        termination=OHMTermination(),
    )

    members: list[OHMMember] = []
    sub_harnesses: dict[str, OHMManifest] = {}
    prev_roles: list[str] = []  # the predecessor stage (the previous step's emitted roles)
    n_agents = 0

    for num, content in _chapter_steps(resolved.body):
        names, gate, block, optional = _parse_step(content)
        if not names and not gate:
            continue  # a non-pipeline prose line (no agent, no gate) — skip, never mint a member
        emitted: list[str] = []
        for name in names:
            role = slugify(name)
            members.append(
                OHMMember(
                    role=role,
                    kind="agent",
                    manifest_ref=f"org:{owner_organization_id}/{role}@1",
                    subgoal=content,
                    depends_on=list(prev_roles),
                )
            )
            sub_harnesses[role] = build_subharness(
                role, owner_organization_id=owner_organization_id, body=content
            )
            emitted.append(role)
            n_agents += 1
        if len(names) > 1:
            flag("F-PROSE-PARALLEL", "info", f"step {num}: {', '.join(emitted)} run in parallel")
        if optional:
            flag(
                "F-PROSE-OPTIONAL", "info", f"step {num} {emitted[0]} optional; emitted as a member"
            )
        if block:
            flag(
                "F-PROSE-BLOCK",
                "confirm",
                f"step {num} {emitted[0]} declares BLOCK on {block}; runtime skip-guard deferred",
            )
        if gate:
            grole = f"gate-{gate.lower()}"
            # the gate depends on THIS step's agents, or (a standalone gate line) the previous stage
            members.append(
                OHMMember(
                    role=grole,
                    kind="human",
                    human_role="author",
                    depends_on=list(emitted or prev_roles),
                )
            )
            flag("F-PROSE-GATE", "confirm", f"GATE {gate} → human node {grole} (human_role=author)")
            emitted = [grole]  # the gate is a barrier — the next step waits on it
        prev_roles = emitted

    n_gates = sum(1 for m in members if m.kind == "human")
    flag(
        "F-PROSE-CHAPTER",
        "info",
        f"prose chapter pipeline parsed → {n_agents} agents + {n_gates} gates",
    )
    flag(
        "F-PROSE-DEFERRED",
        "info",
        "non-chapter subcommands (status/index/validate/produce/market/calibrate/panel) not parsed",
    )
    return OrchestratorPlan(
        orchestration=orchestration, members=members, sub_harnesses=sub_harnesses, flags=flags
    )
