"""Parse a ``.claude/agents/*.md`` into an ``AgentDefinition`` (issue #405; ADR-034 §1).

The real ``.claude/agents`` format is loosely specified, so the parser is tolerant: ``tools`` may
be a YAML list or a comma string; ``skills`` is optional; the body after the frontmatter is the
agent's system prompt. It is pure (text-in, structure-out) and fails CLOSED on absent frontmatter
or a missing ``name`` — the importer flags, never guesses (ADR-034 negatives).

The frontmatter->OHM-v1.1-member mapping that consumes this is a later slice (#406).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.errors import OHMImportError


class AgentDefinition(BaseModel):
    """Parsed form of one ``.claude/agents/<name>.md`` — frontmatter fields + body prompt."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    description: str = ""
    model: str | None = None
    tools: list[str] = Field(default_factory=list)  # the capability ceiling (ADR-032), pre-mapping
    skills: list[str] = Field(default_factory=list)
    body: str = ""  # the markdown after the frontmatter = the agent's system prompt
    source: str = "<unknown>"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a frontmatter document into (frontmatter mapping, body). Fail-closed if absent."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        raise OHMImportError("no YAML frontmatter (expected a leading '---' fence)")
    # drop the leading fence, then split on the closing fence
    rest = stripped[len("---") :].lstrip("\n")
    parts = rest.split("\n---", 1)
    if len(parts) != 2:
        raise OHMImportError("unterminated YAML frontmatter (missing closing '---' fence)")
    fm_text, body = parts
    try:
        front = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise OHMImportError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(front, dict):
        raise OHMImportError("frontmatter must be a YAML mapping")
    return front, body.lstrip("\n")


def _as_str_list(value: Any) -> list[str]:
    """Normalize ``tools``/``skills`` (a YAML list, a comma string, or absent) -> list[str]."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise OHMImportError(f"expected a list or comma-string, got {type(value).__name__}")


def parse_agent_text(text: str, source: str = "<unknown>") -> AgentDefinition:
    """Parse the text of a ``.claude/agents/*.md`` agent into an ``AgentDefinition``."""
    front, body = _split_frontmatter(text)
    if not front.get("name"):
        raise OHMImportError(f"{source}: agent frontmatter is missing a 'name'")
    return AgentDefinition(
        name=str(front["name"]),
        description=str(front.get("description", "")),
        model=(str(front["model"]) if front.get("model") is not None else None),
        tools=_as_str_list(front.get("tools")),
        skills=_as_str_list(front.get("skills")),
        body=body.strip(),
        source=source,
    )


def parse_agent_file(path: str | Path) -> AgentDefinition:
    """Read and parse a ``.claude/agents/*.md`` file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise OHMImportError(f"cannot read agent file {p}: {exc}") from exc
    return parse_agent_text(text, source=p.name)


def discover_agents(agents_dir: str | Path) -> list[Path]:
    """Return the ``*.md`` agent files in a ``.claude/agents`` directory, sorted by name."""
    d = Path(agents_dir)
    if not d.is_dir():
        raise OHMImportError(f"agents directory does not exist: {d}")
    return sorted(d.glob("*.md"))
