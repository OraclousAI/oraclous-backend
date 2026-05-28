"""Redis cache keys isolate organisations on the real harness (ORA-16 / A1, AC#4).

RED until `backend-implementer` adds `oraclous_substrate.cache_keys`.

Reshape (lift-tag **Reshape**) of ``query_cache_service.py`` — whose key was
``qcache:{graph_id}:{hash}`` and whose invalidation scanned ``qcache:{graph_id}:*``
— to prefix ``organisation_id`` as the outer scope. The key-format / non-collision
/ fail-closed unit coverage lives in
``packages/substrate/tests/unit/test_cache_keys_org_scoping.py``; this proves the
isolation holds against a real Redis server and that org-scoped invalidation does
not touch another org's keys.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"
RETRIEVER = "graphrag"


def _scan_delete(redis_client, pattern: str) -> int:
    deleted = 0
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            deleted += redis_client.delete(*keys)
        if cursor == 0:
            return deleted


def test_two_organisations_do_not_share_a_cache_entry(redis_client) -> None:
    from oraclous_substrate.cache_keys import query_cache_key, query_cache_pattern

    graph = f"graph-{uuid.uuid4()}"
    key_a = query_cache_key(ORG_A, graph, "same query", RETRIEVER)
    key_b = query_cache_key(ORG_B, graph, "same query", RETRIEVER)
    try:
        redis_client.set(key_a, "answer-A")
        redis_client.set(key_b, "answer-B")
        assert redis_client.get(key_a) == "answer-A"
        assert redis_client.get(key_b) == "answer-B"  # org B never read org A's entry
    finally:
        _scan_delete(redis_client, query_cache_pattern(ORG_A, graph))
        _scan_delete(redis_client, query_cache_pattern(ORG_B, graph))


def test_org_scoped_invalidation_leaves_other_orgs_untouched(redis_client) -> None:
    from oraclous_substrate.cache_keys import query_cache_key, query_cache_pattern

    graph = f"graph-{uuid.uuid4()}"
    a_keys = [query_cache_key(ORG_A, graph, f"q{i}", RETRIEVER) for i in range(3)]
    b_keys = [query_cache_key(ORG_B, graph, f"q{i}", RETRIEVER) for i in range(3)]
    try:
        for k in a_keys + b_keys:
            redis_client.set(k, "v")

        deleted = _scan_delete(redis_client, query_cache_pattern(ORG_A))

        assert deleted == len(a_keys)
        assert all(redis_client.get(k) is None for k in a_keys)
        assert all(redis_client.get(k) == "v" for k in b_keys)  # org B survives org A's flush
    finally:
        _scan_delete(redis_client, query_cache_pattern(ORG_A, graph))
        _scan_delete(redis_client, query_cache_pattern(ORG_B, graph))
