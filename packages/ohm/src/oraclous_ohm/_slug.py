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

__all__ = ["tool_slug"]


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
