"""Shared YAML-frontmatter splitter (ADR-034).

``.claude/agents/*.md`` and ``.claude/skills/<name>/SKILL.md`` share the same grammar — a leading
``---`` fence, a YAML mapping, a closing ``---`` fence, then a markdown body. This is the one place
that grammar lives, so the agent parser (#405) and the skill resolver (#406) stay in lockstep. Pure;
fail-closed (a missing/unterminated fence or a non-mapping frontmatter raises ``OHMImportError``).
"""

from __future__ import annotations

from typing import Any

import yaml

from oraclous_ohm.errors import OHMImportError


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a frontmatter document into (frontmatter mapping, body). Fail-closed if absent."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        raise OHMImportError("no YAML frontmatter (expected a leading '---' fence)")
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
