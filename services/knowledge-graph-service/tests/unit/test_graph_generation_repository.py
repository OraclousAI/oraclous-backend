"""Unit tests for the KGS per-graph generation bump (#308).

The bump is the KGS→retriever cache-invalidation seam: it INCRs a neutral per-graph generation
counter (``graph_generation_key``) the retriever folds into its cache key. These cover that the bump
lands at the org+graph-scoped key, increments, and — being advisory — swallows a Redis error so a
completed ingest never fails on a cache-invalidation hiccup.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_knowledge_graph_service.repositories.graph_generation_repository import (
    GraphGenerationRepository,
)
from oraclous_substrate.cache_keys import graph_generation_key

pytestmark = pytest.mark.unit

_ORG = str(uuid.UUID("00000000-0000-0000-0000-00000000050a"))
_GRAPH = "g1"


class _FakeSyncRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]


def test_bump_increments_the_org_graph_scoped_generation_key() -> None:
    redis = _FakeSyncRedis()
    repo = GraphGenerationRepository(redis)
    repo.bump(organisation_id=_ORG, graph_id=_GRAPH)
    repo.bump(organisation_id=_ORG, graph_id=_GRAPH)
    assert redis.store[graph_generation_key(_ORG, _GRAPH)] == 2


def test_bump_is_advisory_and_swallows_redis_errors() -> None:
    class _BrokenRedis:
        def incr(self, key: str) -> int:
            raise RuntimeError("redis down")

    # Must not raise — a completed ingest cannot be failed by a cache-invalidation hiccup.
    GraphGenerationRepository(_BrokenRedis()).bump(organisation_id=_ORG, graph_id=_GRAPH)
