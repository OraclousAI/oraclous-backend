"""Text chunking (ORAA-4 §21 services layer).

Lifted from the legacy `DocumentPrimitive` decomposition (develop@84152635): split on blank lines,
strip, drop empties — one free-text chunk per paragraph block. Deterministic, zero-dependency. The
recon flagged a real legacy collision risk (chunk unit_ids not namespaced by document); the
write-repository fixes it by deriving globally-unique node ids from graph_id + document + index.
"""

from __future__ import annotations


def chunk_text(text: str) -> list[str]:
    """Split free text into chunks on blank-line boundaries (legacy `\\n\\n` rule)."""
    return [segment.strip() for segment in text.split("\n\n") if segment.strip()]
