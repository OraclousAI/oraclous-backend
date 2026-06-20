"""Parse a Hierarchy-of-Truth / precedence declaration from a root ``AGENTS.md`` / ``CLAUDE.md``
into ``OHMPrecedence`` (A-NEW-3, item 9).

The source convention (as the book studio declares it): a ``## Hierarchy of Truth`` section whose
fenced block lists the layers highest-first, separated by ``>`` —

    ```
    rules/   (...)   ← highest authority
      >  bible/   (...)
      >  outline/TOC.md   (...)
      >  drafts/   (...)
    ```

— or, as a fallback, a ``Read order:`` line separated by ``→``. We CAPTURE the user's declared
ordering and carry it on the manifest; runtime ENFORCEMENT of graph-vs-file truth is E6 — ``graph``
stays ``derived`` (graph-as-truth is an opt-in mode, never imposed). Pure; returns ``None`` when no
declaration exists (never fabricates an ordering).
"""

from __future__ import annotations

import re
from pathlib import Path

from oraclous_ohm.manifest import OHMPrecedence

_SOURCE_FILES = ("AGENTS.md", "CLAUDE.md")
_READ_ORDER_SEP = re.compile(r"\s*(?:>|→|->)\s*")
_LAYER = re.compile(r"^[\w/.\-]+$")  # a path-ish layer identifier (no spaces)
_FILLER = {"relevant", "the", "specific", "pages", "a", "an"}


def _clean_layer(token: str) -> str:
    """Reduce one source token to its path-ish layer id: drop a leading ``>``, a parenthetical
    description / ``← marker``, ALL backticks, Read-order filler words, and edge punctuation."""
    token = re.split(r"[(←]", token.strip().lstrip(">"), maxsplit=1)[0]  # drop parenthetical/marker
    token = token.replace("`", "").strip()  # drop all backticks
    words = [w for w in token.split() if w.lower() not in _FILLER]
    token = words[-1] if words else token  # the path-ish word is last after any filler
    return token.strip(" /.,;:")  # strip surrounding slashes + trailing punctuation


def _from_hierarchy_block(text: str) -> list[str]:
    """The fenced ``` block within ~25 lines after a 'Hierarchy of Truth' heading (book shape)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "hierarchy of truth" not in line.lower():
            continue
        for j in range(i, min(i + 25, len(lines))):
            if not lines[j].strip().startswith("```"):
                continue
            order: list[str] = []
            for raw in lines[j + 1 :]:
                if raw.strip().startswith("```"):
                    return order
                if not raw.strip():
                    continue
                layer = _clean_layer(raw)
                if layer and _LAYER.match(layer):
                    order.append(layer)
                else:  # a non-layer line ends the ordering
                    return order
            return order
    return []


def _from_read_order(text: str) -> list[str]:
    """Fallback: a single ``Read order: a → b → c`` line (needs the colon + ≥2 layers)."""
    for line in text.splitlines():
        if "read order" not in line.lower() or ":" not in line:
            continue
        after = line.split(":", 1)[1]
        order = [_clean_layer(p) for p in _READ_ORDER_SEP.split(after)]
        order = [o for o in order if o and _LAYER.match(o)]
        if len(order) >= 2:  # an ordering is ≥2 layers; a lone token is not precedence
            return order
    return []


def parse_precedence(root: Path) -> OHMPrecedence | None:
    """Read root ``AGENTS.md`` / ``CLAUDE.md`` and extract the declared truth ordering, or None."""
    text: str | None = None
    for name in _SOURCE_FILES:
        candidate = root / name
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="ignore")
            break
    if text is None:
        return None
    order = _from_hierarchy_block(text) or _from_read_order(text)
    if len(order) < 2:  # a precedence ordering is ≥2 layers; never fabricate from a stray token
        return None
    # graph-as-truth is opt-in (never imposed); only an explicit 'graph: authoritative' declaration
    # would flip it — the book declares the discovery graph 'derived/disposable', so keep derived.
    return OHMPrecedence(order=order, graph="derived")
