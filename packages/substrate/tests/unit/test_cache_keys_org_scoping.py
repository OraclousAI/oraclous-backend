"""organisation_id as the outer scope on the Redis cache-key convention (ORA-16 / A1).

RED until `backend-implementer` adds `oraclous_substrate.cache_keys`.

Reshape (lift-tag **Reshape**) of the legacy query-cache key in
``knowledge-graph-builder/app/services/query_cache_service.py``, whose key was
``qcache:{graph_id}:{sha256}`` — ``graph_id`` was the only tenant scope. A1
adds ``organisation_id`` as the *outermost* scope above ``graph_id``, so the
convention becomes ``qcache:{organisation_id}:{graph_id}:{sha256}``.

These tests describe the *behaviour* of the key/pattern builders (ordering,
non-collision, determinism, query normalisation, fail-closed on a missing
organisation), not the hashing mechanism.
"""

from __future__ import annotations

import fnmatch

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.organization_isolation]

ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"
GRAPH = "graph-abc"
QUERY = "who founded the company?"
RETRIEVER = "graphrag"


def _key(
    org: str = ORG_A, graph: str = GRAPH, query: str = QUERY, retriever: str = RETRIEVER
) -> str:
    from oraclous_substrate.cache_keys import query_cache_key

    return query_cache_key(org, graph, query, retriever)


def test_key_is_prefixed_by_organisation_then_graph() -> None:
    """organisation_id is the outermost segment, graph_id the next — the A1 reshape."""
    assert _key().startswith(f"qcache:{ORG_A}:{GRAPH}:")


def test_two_organisations_never_share_a_key() -> None:
    """Same (graph, query, retriever) under two organisations must not collide."""
    assert _key(org=ORG_A) != _key(org=ORG_B)


def test_key_is_deterministic_for_identical_inputs() -> None:
    assert _key() == _key()


def test_query_is_normalised_within_an_organisation() -> None:
    """Whitespace/case variants of the query hit the same key (lifted legacy behaviour)."""
    assert _key(query="  Who FOUNDED the Company?  ") == _key(query="who founded the company?")


def test_retriever_type_differentiates_the_key() -> None:
    assert _key(retriever="graphrag") != _key(retriever="vector")


def test_blank_organisation_is_rejected_fail_closed() -> None:
    """A missing organisation must raise, never silently produce an un-scoped key (ADR-006)."""
    from oraclous_substrate.cache_keys import query_cache_key

    for bad in ("", "   "):
        with pytest.raises(ValueError):
            query_cache_key(bad, GRAPH, QUERY, RETRIEVER)


def test_blank_graph_is_rejected_fail_closed() -> None:
    from oraclous_substrate.cache_keys import query_cache_key

    with pytest.raises(ValueError):
        query_cache_key(ORG_A, "", QUERY, RETRIEVER)


def test_invalidation_pattern_is_scoped_to_the_organisation() -> None:
    """The org-level SCAN pattern matches that org's keys and no other org's."""
    from oraclous_substrate.cache_keys import query_cache_pattern

    pattern_a = query_cache_pattern(ORG_A)
    assert fnmatch.fnmatch(_key(org=ORG_A), pattern_a)
    assert not fnmatch.fnmatch(_key(org=ORG_B), pattern_a)


def test_invalidation_pattern_can_narrow_to_a_graph() -> None:
    """The (org, graph) pattern matches that graph's keys but not a sibling graph's."""
    from oraclous_substrate.cache_keys import query_cache_pattern

    pattern = query_cache_pattern(ORG_A, GRAPH)
    assert fnmatch.fnmatch(_key(org=ORG_A, graph=GRAPH), pattern)
    assert not fnmatch.fnmatch(_key(org=ORG_A, graph="other-graph"), pattern)


def test_graph_pattern_does_not_leak_across_organisations() -> None:
    from oraclous_substrate.cache_keys import query_cache_pattern

    pattern_a = query_cache_pattern(ORG_A, GRAPH)
    assert not fnmatch.fnmatch(_key(org=ORG_B, graph=GRAPH), pattern_a)
