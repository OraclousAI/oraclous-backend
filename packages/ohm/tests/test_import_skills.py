"""Skill resolver/inliner over .claude/skills/<name>/SKILL.md (#406; ADR-034 §3).

Leaf skills are resolved and inlined into a sub-harness prompt; orchestrator skills (those that
spawn subagents) are detected and flagged for #407, never inlined; a missing skill fails closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_.skills import (
    ResolvedSkill,
    classify_skill,
    inline_skills,
    resolve_skill,
    try_resolve_skill,
)

_LEAF = """---
name: bible-keeper
description: Curator of the source of truth. Only this skill writes to bible/.
---
# bible-keeper
You synthesize canon; you do not write prose.
## Write scope
bible/ only.
"""

_ORCH = """---
name: book-calibrate
description: Calibration coordinator.
---
Fans out 5 calib-* agents in parallel and merges their verdicts.
"""


def _write_skill(root: Path, name: str, text: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text)


def test_classify_leaf() -> None:
    kind, signals = classify_skill("bible-keeper", "Curator.", "You synthesize canon, not prose.")
    assert kind == "leaf"
    assert signals == []


@pytest.mark.parametrize(
    "body",
    [
        "Fans out 5 agents.",
        "Then invoke `book-calibrate` for each chapter.",
        "Spawn a subagent per module.",
        "Run the agents in parallel.",
        "Delegate via the Task tool.",
    ],
)
def test_classify_orchestrator(body: str) -> None:
    kind, signals = classify_skill("x", "", body)
    assert kind == "orchestrator"
    assert signals  # the matched signal is recorded for the dry-run audit


def test_resolve_leaf_skill(tmp_path: Path) -> None:
    _write_skill(tmp_path, "bible-keeper", _LEAF)
    rs = resolve_skill("bible-keeper", tmp_path)
    assert isinstance(rs, ResolvedSkill)
    assert rs.kind == "leaf"
    assert rs.skill_name == "bible-keeper"
    assert "You synthesize canon" in rs.body
    assert rs.source == "bible-keeper/SKILL.md"


def test_resolve_orchestrator_skill(tmp_path: Path) -> None:
    _write_skill(tmp_path, "book-calibrate", _ORCH)
    rs = resolve_skill("book-calibrate", tmp_path)
    assert rs.kind == "orchestrator"
    assert rs.orchestrator_signals


def test_try_resolve_missing_returns_none(tmp_path: Path) -> None:
    assert try_resolve_skill("nope", tmp_path) is None


def test_try_resolve_malformed_raises(tmp_path: Path) -> None:
    d = tmp_path / "broken"
    d.mkdir()
    (d / "SKILL.md").write_text("no frontmatter at all")
    with pytest.raises(OHMImportError):
        try_resolve_skill("broken", tmp_path)


def test_resolve_strict_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(OHMImportError):
        resolve_skill("nope", tmp_path)


def test_inline_skills_appends_block(tmp_path: Path) -> None:
    _write_skill(tmp_path, "bible-keeper", _LEAF)
    rs = resolve_skill("bible-keeper", tmp_path)
    out = inline_skills("You curate the bible.", [rs])
    assert out.startswith("You curate the bible.")
    assert "## Available Skills" in out
    assert "### Skill: bible-keeper" in out
    assert "You synthesize canon" in out


def test_inline_skills_empty_returns_unchanged() -> None:
    assert inline_skills("body", []) == "body"


def test_inline_skills_into_empty_prompt(tmp_path: Path) -> None:
    _write_skill(tmp_path, "bible-keeper", _LEAF)
    rs = resolve_skill("bible-keeper", tmp_path)
    out = inline_skills("", [rs])
    assert out.startswith("## Available Skills")
