"""On-team-import content discovery (#522, E6 — the cloud content-in flow).

So an imported team's members retrieve REAL content (not an empty substrate), the importer dir's
content — the author's git-markdown (bible/rules/drafts), the project docs — is discovered + handed
to the batch-ingest endpoint (the importer itself stays PURE/side-effect-free, #409; this only READS
the tree). The team CONFIG (``.claude/`` agents/skills + ``teams/`` charters) is NOT content — it is
excluded, so a charter or agent prompt never lands in the knowledge graph.

RED until #522 [impl] adds ``oraclous_ohm.import_.content.discover_content_files``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _studio(root: Path) -> None:
    # team CONFIG (must be EXCLUDED — not content)
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "scribe.md").write_text("---\nname: scribe\n---\nwrite.\n")
    (root / "teams" / "1-canon").mkdir(parents=True)
    (root / "teams" / "1-canon" / "charter.md").write_text("# Team\n")
    # team CONTENT (must be DISCOVERED)
    (root / "bible").mkdir()
    (root / "bible" / "canon.md").write_text("# Canon\nThe world is round.")
    (root / "rules").mkdir()
    (root / "rules" / "style.md").write_text("# Style\nUse the active voice.")
    (root / "drafts").mkdir()
    (root / "drafts" / "ch1.md").write_text("# Chapter 1\nIt was a dark night.")
    (root / "README.md").write_text("# Studio\nNotes.")


def test_discovers_content_excluding_team_config(tmp_path: Path) -> None:
    """The bible/rules/drafts + root docs are content; ``.claude``/``teams`` config is not."""
    from oraclous_ohm.import_.content import discover_content_files

    _studio(tmp_path)
    files = discover_content_files(tmp_path)
    by_path = {f.path: f for f in files}

    assert set(by_path) == {"bible/canon.md", "rules/style.md", "drafts/ch1.md", "README.md"}
    assert "The world is round." in by_path["bible/canon.md"].content
    assert by_path["bible/canon.md"].source_type == "text"  # markdown → the text ingest path
    # the team config is NEVER content
    assert not any("charter" in p or ".claude" in p or p.startswith("teams/") for p in by_path)


def test_only_text_grade_content_is_discovered(tmp_path: Path) -> None:
    """Discovery targets text-grade content (md/txt/csv/json); a binary blob is not ingested."""
    from oraclous_ohm.import_.content import discover_content_files

    (tmp_path / "notes.md").write_text("hello")
    (tmp_path / "data.csv").write_text("a,b\n1,2\n")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    paths = {f.path for f in discover_content_files(tmp_path)}
    assert paths == {"notes.md", "data.csv"}


def test_an_empty_tree_discovers_nothing(tmp_path: Path) -> None:
    from oraclous_ohm.import_.content import discover_content_files

    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    assert discover_content_files(tmp_path) == []
