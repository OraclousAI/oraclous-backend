"""The served-citation-ids reserved key, shared by the three first-party retrieval connectors.

Issue #743, Contract #735 §CITE. Two obligations leave a retrieval connector and they are NOT the
same thing:

1. The typed ``citation`` #742 stamps on every hit stays INSIDE the result content the model reads.
   That is pass-through, and it needs no code here: the connectors return the retriever's envelope
   unchanged. #642 is the warning behind it — a receipt the model cannot see is a trap, and real
   models were failed for guessing at ids they were never shown.
2. The connector emits ``served_citation_ids``, a RESERVED result key carrying the ``citation_id``
   of every cited hit. The loop pops it before the result is serialised for the model, so it is
   platform state the model never reads and can never write (``data_absent``, #580, is the
   precedent, and ``tool_use.py`` draws the same "no other tool may emit it" boundary).

**The key is emitted only when the result actually cites something**, exactly as ``data_absent`` is
set only on an empty result: a tool result carries no empty platform bookkeeping to pop.

Dedup is keyed on the ``citation_id`` alone, never on the graph a hit came from. §CITE derives that
id from ``(source_system, source_id, revision)``, so one document version mirrored into two
workspaces is ONE source, and two chunks of one document share one id.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: The reserved result key. The loop pops this name; only these connectors may set it.
SERVED_CITATION_IDS_KEY = "served_citation_ids"


def served_citation_ids(hits: Iterable[Any]) -> list[str]:
    """The ``citation_id`` of every cited hit, first-seen order, deduplicated.

    A hit with ``citation: null`` contributes nothing: the record has no source identity (ingested
    before §CITE, or ingested without a ``source``), so it cannot be cited and must never become a
    null entry in the run's served set. A malformed hit is skipped for the same reason — this runs
    on a sibling service's response, so it never assumes the shape it hopes for.
    """
    out: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        citation = hit.get("citation")
        if not isinstance(citation, dict):
            continue
        citation_id = citation.get("citation_id")
        if isinstance(citation_id, str) and citation_id and citation_id not in seen:
            seen.add(citation_id)
            out.append(citation_id)
    return out
