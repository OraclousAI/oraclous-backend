"""The one canonical tool-name normaliser, shared by every on-ramp (#694).

This function used to be written out twice — ``compiler.validate._tool_slug`` (the canonical
copy, hardened by #594) and ``seeds._slug`` (its inlined twin) — and #694's fix needs a third
caller in ``import_.mapping``. The copies existed for a structural reason: ``compiler.validate``
imports ``import_``, so ``import_.mapping`` importing ``compiler.validate`` back would close an
import cycle.

That duplication was not cosmetic, it WAS #694. ``_GRAPH_REMAP`` was keyed on the Claude-Code tool
names (``"Write"``) while the compiler catalog carries lower-cased slugs (``write``), so every
compiled member fell through the remap onto ``core/write@1`` and wrote into the per-org tmp
sandbox on a graph-bound run (team run ``fe548aac``: ~10 KB of deliverables off-graph). Two
on-ramps disagreed about what a tool is called because neither could see the other's answer.

So the function lives HERE, in a leaf module every layer may import. This module must import
nothing from ``oraclous_ohm`` — that property is the whole reason one shared home is possible, and
it is asserted by ``packages/ohm/tests/test_shared_tool_slug.py``.
"""

from __future__ import annotations

import re

__all__ = [
    "FILE_SUBSTRATE_READ_TOOLS",
    "FILE_SUBSTRATE_TOOLS",
    "FILE_SUBSTRATE_WRITE_TOOLS",
    "GRAPH_READ_TOOLS",
    "GRAPH_WRITE_TOOLS",
    "tool_slug",
]

# The membership sets live beside the normaliser for the same reason the normaliser does. They
# were written out in three modules — the compiler validator, the on-ramp catalog, and the run
# directive — plus a fourth statement of the same partition in the remap table, and the argument
# against that is this module's own docstring: two callers disagreed about what a tool is CALLED
# because neither could see the other's answer. Three callers disagreeing about what a file tool
# IS would be the same failure one layer along.
#
# Slugs, always: a caller compares them through ``tool_slug``, so ``Write`` and ``core/write@1``
# both land here.
#: Tools that WRITE into the per-organisation file sandbox — a directory nothing else can see and
#: that does not survive a container recreate.
FILE_SUBSTRATE_WRITE_TOOLS = frozenset({"write", "edit"})
#: Tools that READ that same sandbox. Against an EMPTY one they are #509's Gap 1: the model loops
#: looking for files nobody wrote.
FILE_SUBSTRATE_READ_TOOLS = frozenset({"read", "grep", "glob"})
#: Both halves. ``bash`` is deliberately absent — it is the rare sandbox EXEC fallback (#507), not
#: a deliverable sink, and it is not what #694 reports.
FILE_SUBSTRATE_TOOLS = FILE_SUBSTRATE_WRITE_TOOLS | FILE_SUBSTRATE_READ_TOOLS

#: The graph write side — where a member's deliverable belongs under the cloud default.
GRAPH_WRITE_TOOLS = frozenset({"graph-ingest"})
#: The graph read side: what other members have already published.
GRAPH_READ_TOOLS = frozenset({"knowledge-retriever", "find-similar", "recall-memory"})


def _basic_slug(text: str) -> str:
    """Lowercase, non-alphanumeric runs collapsed to a single ``-``, trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def tool_slug(text: str) -> str:
    """Normalise a tool NAME or a capability REF to one canonical slug so a catalog, a draft and a
    capability remap compare identically — WITHOUT letting a bogus namespace masquerade as a
    surveyed bare tool (#594).

    A trailing ``@version`` is dropped, and ONLY the canonical ``core/`` built-in namespace is
    stripped. If a ``/`` still remains the identifier is NON-canonical (a foreign namespace or a
    nested path): every segment is slugged and kept, joined by ``--`` under an ``ns--`` marker that
    a bare slug can never contain (``_basic_slug`` collapses runs of ``-`` to one). So it can
    neither collapse onto a bare surveyed slug nor onto a *different* foreign namespace — even when
    the namespace is punctuation or emoji that slugging would otherwise erase entirely
    (``😈/web-research`` and ``./web-research`` would both bare-slug to ``web-research`` and slip
    the gate). If ANY segment slugs to empty the identifier is degenerate and ``""`` is returned,
    so it is DROPPED from a catalog and BLOCKS as a drafted tool — never a wildcard.

    Thus ``core/web-research@1.0.0``, ``Web Research`` and ``web-research`` all → ``web-research``;
    ``evil/web-research`` → ``ns--evil--web-research``; ``core/web-search@1.0.0`` → ``web-search``.
    """
    s = text.strip().lower().split("@", 1)[0]
    if s.startswith("core/"):
        s = s[len("core/") :]
    if "/" in s:
        parts = [_basic_slug(seg) for seg in s.split("/")]
        return "ns--" + "--".join(parts) if all(parts) else ""
    return _basic_slug(s)
