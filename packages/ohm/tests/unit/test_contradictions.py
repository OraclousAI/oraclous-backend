"""Unit: the derived, disposable CONTRADICTS index over file-native claims (#512, E6 / ADR-040).

For a file-native team the markdown tree is canonical; ``CONTRADICTS`` is layered OVER it as a
**derived, disposable index** — recomputed from the claims, never the source of truth. ADR-040
decision 3: under ``precedence.graph: derived`` (the default) a contradiction is a FLAG, it does not
invalidate or mutate anything canonical. Deleting the index and rebuilding it loses nothing.

This ports the KGS semantic-contradiction logic (``memory_repository.find_contradictions``:
same subject+predicate with a negation flip, or two non-negated assertions of different objects) to
a pure function with no Neo4j, so the file-native blackboard can flag conflicts without a store.

The seam ``oraclous_ohm.contradictions`` is built by #512 [impl]; importing it function-locally
keeps collection green (RED-at-runtime by ModuleNotFoundError until [impl] lands — §4.1).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _S(subject: str, predicate: str, object_: str, is_negation: bool = False):
    from oraclous_ohm.contradictions import Statement

    return Statement(subject=subject, predicate=predicate, object=object_, is_negation=is_negation)


def test_different_objects_both_asserted_is_a_contradiction() -> None:
    """`Alice is protagonist` vs `Bob is protagonist` — X holds one value of the predicate."""
    from oraclous_ohm.contradictions import find_contradictions

    existing = [_S("protagonist", "is", "Alice")]
    hits = find_contradictions(_S("protagonist", "is", "Bob"), existing)
    assert hits == existing


def test_negation_flip_of_the_same_object_is_a_contradiction() -> None:
    """`X is Y` vs `X is-not Y` — one asserts the object, the other denies the SAME object."""
    from oraclous_ohm.contradictions import find_contradictions

    existing = [_S("setting", "is", "Paris")]
    hits = find_contradictions(_S("setting", "is", "Paris", is_negation=True), existing)
    assert hits == existing


def test_a_different_predicate_is_not_a_contradiction() -> None:
    from oraclous_ohm.contradictions import find_contradictions

    existing = [_S("Alice", "lives_in", "Paris")]
    assert find_contradictions(_S("Alice", "born_in", "Lyon"), existing) == []


def test_two_negations_of_different_objects_are_compatible() -> None:
    """`X is-not Y` and `X is-not Z` can both hold — not a contradiction."""
    from oraclous_ohm.contradictions import find_contradictions

    existing = [_S("ending", "is", "happy", is_negation=True)]
    assert find_contradictions(_S("ending", "is", "tragic", is_negation=True), existing) == []


def test_the_index_is_derived_and_disposable() -> None:
    """Recomputing the index from the same claims yields the same result and mutates nothing —
    deleting the index loses nothing canonical (the claims are untouched)."""
    from oraclous_ohm.contradictions import build_contradiction_index

    claims = [
        _S("protagonist", "is", "Alice"),
        _S("protagonist", "is", "Bob"),
        _S("setting", "is", "Paris"),
    ]
    snapshot = list(claims)

    first = build_contradiction_index(claims)
    second = build_contradiction_index(claims)  # "delete + rebuild"

    assert first == second  # deterministic / rebuildable
    assert first, "Alice-vs-Bob protagonist clash must be in the derived index"
    assert claims == snapshot  # the canonical claims were never mutated by index-building
