"""On-team-import content discovery (#522, E6 — the cloud content-in flow).

A PURE read of a team dir's content so a client can batch-ingest it into the org graph on import —
the members then retrieve REAL content, not an empty substrate. Only text-grade content
(``.md``/``.markdown``/``.txt``/``.csv``/``.json``) is discovered; the team CONFIG (``.claude/``
agents/skills + ``teams/`` charters) is EXCLUDED, so a charter or agent prompt never lands in the
knowledge graph. No side effects — the importer itself stays pure (#409); this only reads the tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Text-grade content the batch-ingest path handles (book/EURail/DoefinGPT corpora are text);
# a binary blob (images, archives) is not text-ingested here — the single /upload path handles it.
_TEXT_EXTS = {".md", ".markdown", ".txt", ".csv", ".json"}
# Top-level dirs that are team CONFIG, never content: the importer's own inputs.
_EXCLUDE_DIRS = {".claude", "teams"}


@dataclass(frozen=True)
class ContentFile:
    """One discovered content file: its relative POSIX ``path`` (the document identity — re-ingest
    of the same path replaces, idempotent), the text ``content``, and the KGS ``source_type``."""

    path: str
    content: str
    source_type: str


def _source_type_for(suffix: str) -> str:
    """The KGS ingest source_type for a text-grade extension (markdown/txt → the free-text path)."""
    if suffix == ".csv":
        return "csv"
    if suffix == ".json":
        return "json"
    return "text"


def discover_content_files(root: str | Path) -> list[ContentFile]:
    """Discover text-grade content under ``root``, EXCLUDING the ``.claude/``/``teams/`` config.

    Pure + deterministic (sorted by path). A file whose top-level dir is config, whose extension is
    not text-grade, or whose bytes are not UTF-8 text is skipped. Returns ``ContentFile`` items a
    client feeds to ``POST /api/v1/graphs/{id}/batch-ingest``."""
    base = Path(root)
    out: list[ContentFile] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if rel.parts[0] in _EXCLUDE_DIRS:  # team config, never content
            continue
        if path.suffix.lower() not in _TEXT_EXTS:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # not text-grade
        out.append(
            ContentFile(
                path=rel.as_posix(),
                content=content,
                source_type=_source_type_for(path.suffix.lower()),
            )
        )
    return out
