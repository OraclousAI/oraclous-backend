"""Hierarchy-of-Truth precedence resolution — the pure substrate-agnostic core (#514, E6 / ADR-040).

The runtime ADOPTS + ENFORCES the source's truth ordering (rules > bible > TOC > drafts): on a
contradiction the higher-precedence source wins, the DERIVED graph is NOT canonical (a contradicting
graph/derived fact never overrides a file-tier layer unless ``graph="authoritative"`` is explicitly
declared), and — the book invariant (item-9/§22) — a member can NEVER self-assign a canonical tier
(it would invert canonical truth to graph-as-truth). Precedence (NOT recency — unlike the KGS-memory
new-wins rule) decides; a same-tier contradiction is FLAGGED, never silently picked.

Tier is PATH-DERIVED, not an ingest field (CTO ruling): a fact's tier is the ``precedence.order``
entry that prefixes its source path; no match → ``"graph"`` (derived). This is the load-bearing
``path→tier`` map, pure + in ``packages/ohm`` where ``OHMPrecedence`` already lives.

RED until #514 [impl] adds ``oraclous_ohm.precedence_resolution`` (+ ``Statement.source`` +
``HandoffEnvelope.source_layer``). New-seam imports are function-local (§4.1 — the seam doesn't
exist yet, so a module-level import would red-abort collection for every open PR).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# rules > bible > TOC > drafts (highest-first); path-ish dir ids, as parse_precedence emits.
_ORDER = ["rules", "bible", "toc", "drafts"]


def _stmt(subject: str, predicate: str, obj: str, *, source: str | None = None, neg: bool = False):
    """A Statement tagged with its source tier (Statement.source is additive, #514)."""
    from oraclous_ohm.contradictions import Statement

    return Statement(
        subject=subject, predicate=predicate, object=obj, is_negation=neg, source=source
    )


# ------------------------------------------------------------- path → tier (the load-bearing map)


def test_tier_is_the_order_entry_prefixing_the_source_path() -> None:
    """A content node's tier = the ``order`` entry that prefixes its source path (pure path map)."""
    from oraclous_ohm.precedence_resolution import tier_for_path

    assert tier_for_path("rules/policy.md", _ORDER) == "rules"
    assert tier_for_path("bible/canon.md", _ORDER) == "bible"
    assert tier_for_path("drafts/ch1.md", _ORDER) == "drafts"


def test_a_path_under_no_declared_layer_is_the_derived_graph_tier() -> None:
    """A path under no declared layer / not traceable to a source path → ``graph`` (derived)."""
    from oraclous_ohm.precedence_resolution import tier_for_path

    assert tier_for_path("scratch/notes.md", _ORDER) == "graph"
    assert tier_for_path("", _ORDER) == "graph"


# ----------------------------------------------------------------- resolution ordering


def test_higher_tier_wins_a_contradiction_rules_over_drafts() -> None:
    from oraclous_ohm.precedence_resolution import resolve_by_precedence

    rules = _stmt("ending", "is", "happy", source="rules")
    drafts = _stmt("ending", "is", "tragic", source="drafts")
    assert resolve_by_precedence(rules, drafts, _ORDER) is rules
    assert resolve_by_precedence(drafts, rules, _ORDER) is rules  # order of args irrelevant


def test_precedence_is_transitive_bible_beats_toc_beats_drafts() -> None:
    from oraclous_ohm.precedence_resolution import resolve_by_precedence

    bible = _stmt("hero", "is", "knight", source="bible")
    toc = _stmt("hero", "is", "mage", source="toc")
    drafts = _stmt("hero", "is", "thief", source="drafts")
    assert resolve_by_precedence(bible, toc, _ORDER) is bible
    assert resolve_by_precedence(toc, drafts, _ORDER) is toc
    assert resolve_by_precedence(bible, drafts, _ORDER) is bible


# ----------------------------------------------------------------- graph derived-not-canonical


def test_a_derived_graph_fact_never_overrides_bible() -> None:
    """The explicit acceptance case: under the default ``graph="derived"`` a contradicting graph
    node does NOT override a file-tier (bible) node — bible wins."""
    from oraclous_ohm.precedence_resolution import resolve_by_precedence

    bible = _stmt("city", "is", "Paris", source="bible")
    graph = _stmt("city", "is", "London", source="graph")
    assert resolve_by_precedence(bible, graph, _ORDER, graph_authoritative=False) is bible


def test_graph_authoritative_is_honored_only_when_explicitly_declared() -> None:
    """``graph="authoritative"`` lets a graph fact win; the DEFAULT (derived) never does."""
    from oraclous_ohm.precedence_resolution import resolve_by_precedence

    bible = _stmt("city", "is", "Paris", source="bible")
    graph = _stmt("city", "is", "London", source="graph")
    # declared authoritative → graph may win
    assert resolve_by_precedence(bible, graph, _ORDER, graph_authoritative=True) is graph
    # default derived → bible always wins (never the graph)
    assert resolve_by_precedence(bible, graph, _ORDER) is bible


# ----------------------------------------------------------------- same-tier → flag, never silent


def test_a_same_tier_contradiction_is_flagged_not_silently_picked() -> None:
    """Two contradicting facts at the SAME tier are not auto-resolved (precedence can't rank them) —
    the resolver flags (returns None), it never silently picks a winner."""
    from oraclous_ohm.precedence_resolution import resolve_by_precedence

    a = _stmt("ending", "is", "happy", source="drafts")
    b = _stmt("ending", "is", "tragic", source="drafts")
    assert resolve_by_precedence(a, b, _ORDER) is None


# ----------------------------------------------------------------- THE BOOK INVARIANT: the clamp


def test_a_member_cannot_self_assign_a_canonical_tier_it_is_clamped() -> None:
    """Item-9/§22 invariant: rules/bible/toc are the SOURCE's, assigned ONLY from real source-path
    provenance — a member's self-declared canonical tier is clamped to the non-canonical floor, so a
    member can never invert canon by declaring ``source="bible"``."""
    from oraclous_ohm.precedence_resolution import clamp_member_source

    assert clamp_member_source("bible", _ORDER) == "drafts"  # canonical claim clamped to the floor
    assert clamp_member_source("rules", _ORDER) == "drafts"
    assert clamp_member_source("drafts", _ORDER) == "drafts"  # already the floor — unchanged
    assert clamp_member_source(None, _ORDER) == "drafts"  # default fail-closed to the lowest tier


def test_clamp_means_a_member_claim_loses_to_a_real_canonical_fact() -> None:
    """End-to-end of the clamp: a member that DECLARED bible is clamped to drafts and loses to a
    real (path-provenanced) bible fact — graph-as-truth inversion is impossible."""
    from oraclous_ohm.precedence_resolution import clamp_member_source, resolve_by_precedence

    real_bible = _stmt("city", "is", "Paris", source="bible")
    member_claim = _stmt("city", "is", "London", source=clamp_member_source("bible", _ORDER))
    assert resolve_by_precedence(real_bible, member_claim, _ORDER) is real_bible


# ----------------------------------------------------------------- non-contradicting pass-through


def test_non_contradicting_facts_are_never_dropped() -> None:
    """Compatible facts across layers are untouched — resolution fires only on a real conflict."""
    from oraclous_ohm.precedence_resolution import resolve_by_precedence

    a = _stmt("hero", "is", "brave", source="bible")
    b = _stmt("city", "is", "Paris", source="drafts")  # different subject → not a contradiction
    # no contradiction → the resolver leaves both (returns None meaning "nothing to resolve")
    assert resolve_by_precedence(a, b, _ORDER) is None


# ------------------------------------------------------------- the additive carriers (#520 trap)


def test_statement_carries_an_optional_source_tier_default_none() -> None:
    from oraclous_ohm.contradictions import Statement

    assert (
        Statement(subject="x", predicate="is", object="y").source is None
    )  # additive, default None
    assert Statement(subject="x", predicate="is", object="y", source="bible").source == "bible"


def test_handoff_envelope_carries_an_optional_source_layer_default_none() -> None:
    from oraclous_ohm.envelope import HandoffEnvelope

    assert (
        HandoffEnvelope(from_role="a", to_role="b").source_layer is None
    )  # additive, default None
    assert (
        HandoffEnvelope(from_role="a", to_role="b", source_layer="drafts").source_layer == "drafts"
    )
