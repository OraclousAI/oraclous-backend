"""Derived, disposable contradiction detection over file-native claims (#512, E6 / ADR-040).

For a **file-native** team the markdown tree is canonical; ``CONTRADICTS`` is layered OVER it as a
*derived, disposable index* — recomputed from the claims, never the source of truth. ADR-040
decision 3: a contradiction is a **flag**; under the default ``precedence.graph: derived`` it never
invalidates or mutates anything canonical, and precedence (not recency) decides which source wins.

This ports the KGS semantic-contradiction rule (``memory_repository.find_contradictions``) to a pure
function with no Neo4j and no persistence, so the file-native blackboard can flag conflicts (the
``book-integrity`` check) while the files stay canonical. The graph-resident store + its
``record_contradiction(mode=…)`` canonicity gate are the graph-adopt substrate's concern (#513/14).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Statement:
    """A semantic claim extracted from the canonical tree: ``<subject> <predicate> <object>``.

    ``is_negation`` distinguishes ``X is Y`` from ``X is-not Y``. Frozen → hashable + value-equal,
    so a derived index of contradicting pairs is a plain set (rebuildable + disposable)."""

    subject: str
    predicate: str
    object: str  # noqa: A003 — the claim's object; the triple's third term, not the builtin
    is_negation: bool = False
    source: str | None = None  # the fact's tier/source path (additive, #514); find_contradictions
    # keys only on subject/predicate/object/is_negation, so source never changes contradiction eq.


def find_contradictions(new: Statement, existing: Iterable[Statement]) -> list[Statement]:
    """The existing claims ``new`` genuinely contradicts (ported from KGS, ADR-027 §1).

    Same subject + same predicate, and EITHER:
      * a same-object negation FLIP — ``X is Y`` vs ``X is-not Y`` (one asserts, one denies the SAME
        object); OR
      * a different-object clash between TWO NON-negated assertions — ``X is Y`` vs ``X is Z`` (the
        subject can hold only one value of this predicate).
    Two negations of different objects, or an assertion vs a negation of a *different* object, are
    compatible — not contradictions.
    """
    hits: list[Statement] = []
    for s in existing:
        if s.subject != new.subject or s.predicate != new.predicate:
            continue
        negation_flip = s.object == new.object and s.is_negation != new.is_negation
        different_object_clash = (
            s.object != new.object and not s.is_negation and not new.is_negation
        )
        if negation_flip or different_object_clash:
            hits.append(s)
    return hits


def build_contradiction_index(statements: Iterable[Statement]) -> frozenset[frozenset[Statement]]:
    """The derived, disposable index: the set of contradicting claim PAIRS over ``statements``.

    Pure and deterministic — rebuilding it from the same claims yields the same set, and it never
    mutates the claims, so deleting the index loses nothing canonical (ADR-040 decision 3). The
    contradiction relation is symmetric, so each unordered pair is recorded once."""
    claims = list(statements)
    pairs: set[frozenset[Statement]] = set()
    for i, s in enumerate(claims):
        for other in claims[i + 1 :]:
            if find_contradictions(s, [other]):
                pairs.add(frozenset({s, other}))
    return frozenset(pairs)
