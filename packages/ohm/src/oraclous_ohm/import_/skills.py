"""Resolve a ``.claude/skills/<name>/SKILL.md`` and inline a LEAF skill into a sub-harness prompt.

ADR-034 §3. A leaf skill's instructions are inlined verbatim into the generating sub-harness's
primary prompt (no skill re-authored). A skill that itself spawns subagents (an ORCHESTRATOR) is
detected and flagged for the #407 single-skill-orchestrator adapter — never flattened into a prompt.
Pure; fail-closed. Conservative bias: when a skill could be either, classify orchestrator and flag.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_._frontmatter import split_frontmatter

SkillKind = Literal["leaf", "orchestrator"]

# A skill is an ORCHESTRATOR if its text shows it spawns/fans-out subagents (Task-tool delegation).
# Conservative — any match flags orchestrator (a false positive is a confirm-flag; a false negative
# inlines subagent-spawning instructions into a leaf prompt, which is unsound).
_ORCHESTRATOR_PATTERNS = [
    re.compile(r"\bfan(s|ned)?\s+out\b", re.IGNORECASE),
    re.compile(r"\binvoke\s+`[a-z0-9][a-z0-9-]*`", re.IGNORECASE),
    re.compile(r"\bparallel\b.{0,40}\b(agent|step|skill)s?\b", re.IGNORECASE),
    re.compile(r"\b(agent|step|skill)s?\b.{0,40}\bparallel\b", re.IGNORECASE),
    re.compile(r"\bspawn(s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bsub-?agents?\b", re.IGNORECASE),
    re.compile(r"\bTask\s+tool\b", re.IGNORECASE),
]

_SKILLS_HEADER = "## Available Skills"
_SKILLS_INTRO = (
    "The following skill capabilities are available to you. Follow their "
    "instructions when the task calls for them."
)


class ResolvedSkill(BaseModel):
    """A resolved skill: leaf instructions (inlinable) or a flagged orchestrator."""

    model_config = ConfigDict(extra="ignore")

    name: str  # the skills[] entry as requested
    kind: SkillKind
    skill_name: str  # frontmatter `name` (may differ from the requested name)
    description: str = ""
    body: str = ""  # the markdown after the frontmatter (the inlinable instructions)
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    orchestrator_signals: list[str] = Field(default_factory=list)
    source: str = ""  # the SKILL.md path relative to skills_root


def classify_skill(skill_name: str, description: str, body: str) -> tuple[SkillKind, list[str]]:
    """Return (kind, matched signal strings). Orchestrator if any spawn/fan-out signal matches."""
    text = f"{description}\n{body}"
    signals = [m.group(0) for pat in _ORCHESTRATOR_PATTERNS if (m := pat.search(text))]
    return ("orchestrator" if signals else "leaf"), signals


def try_resolve_skill(name: str, skills_root: str | Path) -> ResolvedSkill | None:
    """Resolve ``<skills_root>/<name>/SKILL.md``. None if absent; raise on a malformed file."""
    root = Path(skills_root)
    skill_md = root / name / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        raise OHMImportError(f"cannot read skill {name!r}: {exc}") from exc
    front, body = split_frontmatter(text)  # raises OHMImportError on a malformed file
    skill_name = str(front.get("name") or name)
    description = " ".join(str(front.get("description", "")).split())
    kind, signals = classify_skill(skill_name, description, body)
    return ResolvedSkill(
        name=name,
        kind=kind,
        skill_name=skill_name,
        description=description,
        body=body.strip(),
        frontmatter=front,
        orchestrator_signals=signals,
        source=str(skill_md.relative_to(root)),
    )


def resolve_skill(name: str, skills_root: str | Path) -> ResolvedSkill:
    """Strict resolve — raise ``OHMImportError`` if the skill is absent or malformed."""
    resolved = try_resolve_skill(name, skills_root)
    if resolved is None:
        raise OHMImportError(f"skill {name!r} not found under {skills_root}")
    return resolved


def inline_skills(prompt_body: str, resolved: list[ResolvedSkill]) -> str:
    """Append leaf skills to a sub-harness primary prompt under an '## Available Skills' block."""
    if not resolved:
        return prompt_body
    sections = [
        f"### Skill: {rs.skill_name}\n\n"
        f"**Metadata:**\n- Name: {rs.skill_name}\n- Description: {rs.description}\n\n"
        f"{rs.body}"
        for rs in resolved
    ]
    block = f"{_SKILLS_HEADER}\n\n{_SKILLS_INTRO}\n\n" + "\n\n".join(sections)
    if not prompt_body.strip():
        return block
    return f"{prompt_body}\n\n{block}"
