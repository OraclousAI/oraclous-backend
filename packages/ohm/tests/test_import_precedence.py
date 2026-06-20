"""parse_precedence — Hierarchy-of-Truth extraction (item 9 source capture).

Pins the parser: the book's fenced ``## Hierarchy of Truth`` block, the ``Read order:`` fallback,
and fail-soft (no file / no declaration -> None, never fabricated).
"""

from __future__ import annotations

from pathlib import Path

from oraclous_ohm.import_.precedence import parse_precedence


def test_parses_the_hierarchy_of_truth_fenced_block(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "## 2. Hierarchy of Truth (conflict resolution order)\n\n"
        "```\n"
        "rules/   (thesis · voice)   ← highest authority\n"
        "  >  bible/   (canonical claims)\n"
        "  >  outline/TOC.md   (the living TOC)\n"
        "  >  drafts/   (prose)\n"
        "```\n"
    )
    prec = parse_precedence(tmp_path)
    assert prec is not None
    assert prec.order == ["rules", "bible", "outline/TOC.md", "drafts"]
    assert prec.graph == "derived"  # graph-as-truth never imposed


def test_parses_the_read_order_fallback_line(tmp_path: Path) -> None:
    # no fenced hierarchy block — fall back to a "Read order:" line with → separators + backticks.
    (tmp_path / "CLAUDE.md").write_text(
        "## Conventions\n\n"
        "- Read order: `rules/` → relevant `bible/` pages → `outline/TOC.md` → the `drafts/`.\n"
    )
    prec = parse_precedence(tmp_path)
    assert prec is not None
    assert prec.order == ["rules", "bible", "outline/TOC.md", "drafts"]


def test_no_source_file_returns_none(tmp_path: Path) -> None:
    assert parse_precedence(tmp_path) is None  # no AGENTS.md / CLAUDE.md


def test_no_declaration_returns_none(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# A book\n\nJust prose, no hierarchy, no read order.\n")
    assert parse_precedence(tmp_path) is None  # never fabricated
