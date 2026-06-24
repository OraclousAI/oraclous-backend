"""Hierarchy-of-Truth precedence resolution — the pure substrate-agnostic core (#514, E6 / ADR-040).

The runtime ADOPTS + ENFORCES the source's declared truth ordering (e.g. ``rules > bible > TOC >
drafts``): on a genuine contradiction the higher-precedence source wins, the DERIVED graph is NOT
canonical (a fact whose tier is not a declared layer never overrides a file-tier layer unless the
manifest declares ``graph="authoritative"``), and — the item-9/§22 book invariant — a member can
NEVER self-assign a canonical tier. Precedence (NOT recency — unlike the KGS-memory new-wins rule)
decides; a same-tier contradiction is FLAGGED (returned unresolved), never silently picked.

Tier is PATH-DERIVED (CTO ruling): a fact's tier is the ``precedence.order`` entry that prefixes its
source path; no match → ``"graph"`` (derived). Pure + I/O-free; lives in ``packages/ohm`` beside
``OHMPrecedence`` and reuses ``find_contradictions`` (the #512 contradiction relation), so
precedence logic never leaks into a service.
"""

from __future__ import annotations

from oraclous_ohm.contradictions import Statement, find_contradictions

_GRAPH_TIER = "graph"


def tier_for_path(path: str, order: list[str]) -> str:
    """A content node's tier = the first ``order`` entry prefixing its source path; else ``graph``.

    Matches a whole path segment only (``rules`` matches ``rules`` or ``rules/...``, never
    ``rulesy/...``), so a derived node with no declared-layer provenance falls through to ``graph``.
    """
    for entry in order:
        if path == entry or path.startswith(entry + "/"):
            return entry
    return _GRAPH_TIER


def _rank(source: str | None, order: list[str], *, graph_authoritative: bool) -> float:
    """Lower rank = higher precedence. A declared-layer source → its index in ``order``. A derived
    source (``None`` / ``graph`` / not a declared layer) → ``-inf`` when the graph is authoritative
    (it may win), else ``+inf`` (it always loses to any declared file tier — derived-not-canonical).
    """
    if source is not None and source in order:
        return float(order.index(source))
    return float("-inf") if graph_authoritative else float("inf")


def resolve_by_precedence(
    a: Statement, b: Statement, order: list[str], *, graph_authoritative: bool = False
) -> Statement | None:
    """Resolve a contradiction between two tier-tagged statements by precedence.

    Returns the higher-precedence statement BY REFERENCE (one of the inputs, never a copy). A
    same-tier contradiction returns ``None`` (precedence cannot rank it — FLAG, never silently
    pick); two statements that do not genuinely contradict also return ``None`` (nothing to do).
    """
    if not find_contradictions(a, [b]):  # not a contradiction → leave both, resolve nothing
        return None
    ra = _rank(a.source, order, graph_authoritative=graph_authoritative)
    rb = _rank(b.source, order, graph_authoritative=graph_authoritative)
    if ra == rb:  # same tier — precedence cannot rank; flag, do not pick
        return None
    return a if ra < rb else b  # identity preserved — the winning input, never a rebuilt object


def clamp_member_source(declared: str | None, order: list[str]) -> str:
    """The item-9/§22 book invariant: a member can NEVER self-assign a canonical tier.

    Clamp UNCONDITIONALLY to the non-canonical floor (the lowest-precedence entry, ``order[-1]``)
    regardless of what the member ``declared`` — canonical tiers (rules/bible/toc) are the SOURCE's,
    assigned only from real source-path provenance, never a member's own claim. Empty order →
    ``graph`` (fail-closed non-canonical).
    """
    return order[-1] if order else _GRAPH_TIER
