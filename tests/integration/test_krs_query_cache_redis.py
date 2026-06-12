"""End-to-end KRS query-cache invalidation over a real Redis (#308).

Proves the KGS↔retriever cache-invalidation seam holds against a real Redis container: the
retriever-side cache read (``QueryCacheRepository`` via ``RetrievalService``) and the KGS-side
generation bump (``GraphGenerationRepository``) agree on the neutral ``graph_generation_key``
convention — a real ``INCR`` turns the next retriever read into a cache-miss, without either side
knowing the other's key layout. Lives under ``tests/`` so it consumes the shared substrate
``redis_async_client`` fixture (the service-dir integration suites use in-memory fakes).
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_graph_service.repositories.graph_generation_repository import (
    GraphGenerationRepository,
)
from oraclous_knowledge_retriever_service.services.embedder import HashingEmbedder
from oraclous_knowledge_retriever_service.services.retrieval_service import RetrievalService

pytestmark = pytest.mark.integration

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")


def _ctx():
    return use_organisation_context(
        OrganisationContext(
            organisation_id=_ORG, principal_id=_ORG, principal_type=PrincipalType.USER
        )
    )


class _FakeRecord:
    def __init__(self, d: dict) -> None:
        self._d = d

    def data(self) -> dict:
        return self._d


class _CountingDriver:
    """Counts how many Cypher reads it served (the cache should suppress repeats)."""

    def __init__(self, row: dict) -> None:
        self._row = row
        self.calls = 0

    def execute_query(self, cypher, **kw):
        self.calls += 1
        return ([_FakeRecord(self._row)], None, None)


async def test_search_hit_then_kgs_generation_bump_invalidates(redis_async_client) -> None:
    row = {"id": "c1", "labels": ["Chunk"], "props": {"text": "ada"}, "score": 0.9}
    driver = _CountingDriver(row)
    svc = RetrievalService(driver, HashingEmbedder(8), redis_client=redis_async_client)
    with _ctx():
        await svc.semantic(graph_id="g1", query="who wrote it", top_k=10)  # miss -> caches
        await svc.semantic(graph_id="g1", query="who wrote it", top_k=10)  # served from real Redis
        assert driver.calls == 1

    # The KGS bumps the per-graph generation through its OWN repository (the production code path)
    # over the same real Redis — never a retriever cache key. The retriever folds the new generation
    # into its cache key, so the next read misses.
    GraphGenerationRepository.bump_for(
        redis_url=_redis_url(redis_async_client), organisation_id=str(_ORG), graph_id="g1"
    )

    with _ctx():
        await svc.semantic(graph_id="g1", query="who wrote it", top_k=10)
    assert driver.calls == 2


async def test_subgraph_cached_then_invalidated(redis_async_client) -> None:
    row = {
        "nodes": [{"id": "n1", "labels": ["Document"], "props": {"name": "A"}}],
        "edge_groups": [[]],
    }
    driver = _CountingDriver(row)
    svc = RetrievalService(driver, HashingEmbedder(8), redis_client=redis_async_client)
    with _ctx():
        await svc.subgraph(graph_id="g9", limit=50)  # miss
        await svc.subgraph(graph_id="g9", limit=50)  # hit
        assert driver.calls == 1
    GraphGenerationRepository.bump_for(
        redis_url=_redis_url(redis_async_client), organisation_id=str(_ORG), graph_id="g9"
    )
    with _ctx():
        await svc.subgraph(graph_id="g9", limit=50)
    assert driver.calls == 2


def _redis_url(async_client) -> str:
    """Reconstruct a ``redis://`` URL from the async client's connection pool (so the sync KGS
    repository can open its own short-lived client against the same test container)."""
    kwargs = async_client.connection_pool.connection_kwargs
    return f"redis://{kwargs['host']}:{kwargs['port']}/{kwargs.get('db', 0)}"
