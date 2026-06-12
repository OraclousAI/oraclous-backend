"""Unit tests for the KRS Redis query cache (#308).

Cover, with an in-memory fake async Redis (no real Redis — these stay in the `unit` lane):
- cache hit/miss on the search + subgraph reads (a second identical call is served from the cache,
  the underlying repo/driver is hit once);
- the key composition (org + graph + generation + query-hash) — distinct orgs/graphs/queries/
  generations never collide; whitespace/case variants of a query do collide;
- ingest-time invalidation — bumping the per-graph generation counter makes the next call a miss
  that re-hits the driver;
- the enable-flag-off path (a None client) caches nothing — every call re-hits the driver;
- the TTL is applied on `set`.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_retriever_service.repositories.query_cache_repository import (
    QueryCacheRepository,
)
from oraclous_knowledge_retriever_service.services.embedder import HashingEmbedder
from oraclous_knowledge_retriever_service.services.retrieval_service import RetrievalService
from oraclous_substrate.cache_keys import graph_generation_key

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")


def _ctx():
    return use_organisation_context(
        OrganisationContext(
            organisation_id=_ORG, principal_id=_ORG, principal_type=PrincipalType.USER
        )
    )


class _FakeAsyncRedis:
    """A minimal in-memory async stand-in for redis.asyncio.Redis (get/set/incr + ttl capture)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def incr(self, key: str) -> int:
        new = int(self.store.get(key, 0)) + 1
        self.store[key] = str(new)
        return new


class _FakeRecord:
    def __init__(self, d: dict) -> None:
        self._d = d

    def data(self) -> dict:
        return self._d


class _CountingDriver:
    """Returns the same single chunk row and counts how many queries it served."""

    def __init__(self) -> None:
        self.calls = 0

    def execute_query(self, cypher, **kw):
        self.calls += 1
        row = {"id": "c1", "labels": ["Chunk"], "props": {"text": "ada"}, "score": 0.9}
        return ([_FakeRecord(row)], None, None)


def _service(redis_client, driver=None, ttl: int = 300) -> RetrievalService:
    return RetrievalService(
        driver or _CountingDriver(), HashingEmbedder(8), redis_client=redis_client, cache_ttl=ttl
    )


# --- hit / miss ---------------------------------------------------------------


async def test_second_identical_search_is_a_cache_hit() -> None:
    driver = _CountingDriver()
    svc = _service(_FakeAsyncRedis(), driver)
    with _ctx():
        first = await svc.semantic(graph_id="g1", query="who wrote it", top_k=10)
        second = await svc.semantic(graph_id="g1", query="who wrote it", top_k=10)
    assert first == second
    assert driver.calls == 1  # second call served from the cache, the driver was hit once


async def test_subgraph_is_cached() -> None:
    class _SubgraphDriver:
        def __init__(self) -> None:
            self.calls = 0

        def execute_query(self, cypher, **kw):
            self.calls += 1
            row = {
                "nodes": [{"id": "n1", "labels": ["Document"], "props": {"name": "A"}}],
                "edge_groups": [[]],
            }
            return ([_FakeRecord(row)], None, None)

    driver = _SubgraphDriver()
    svc = _service(_FakeAsyncRedis(), driver)
    with _ctx():
        first = await svc.subgraph(graph_id="g1", limit=100)
        second = await svc.subgraph(graph_id="g1", limit=100)
    assert first == second
    assert driver.calls == 1


# --- key composition ----------------------------------------------------------


async def test_different_graphs_do_not_collide() -> None:
    driver = _CountingDriver()
    svc = _service(_FakeAsyncRedis(), driver)
    with _ctx():
        await svc.semantic(graph_id="g1", query="q", top_k=10)
        await svc.semantic(graph_id="g2", query="q", top_k=10)
    assert driver.calls == 2  # a different graph_id is a distinct key -> a miss


async def test_different_query_does_not_collide_but_normalised_variant_does() -> None:
    driver = _CountingDriver()
    svc = _service(_FakeAsyncRedis(), driver)
    with _ctx():
        await svc.fulltext(graph_id="g1", query="ada lovelace", top_k=10)
        await svc.fulltext(graph_id="g1", query="grace hopper", top_k=10)  # different -> miss
        await svc.fulltext(graph_id="g1", query="  ADA Lovelace ", top_k=10)  # variant -> hit
    assert driver.calls == 2


async def test_two_organisations_never_share_a_cached_entry() -> None:
    redis = _FakeAsyncRedis()
    driver_a = _CountingDriver()
    driver_b = _CountingDriver()
    org_b = uuid.UUID("11111111-1111-1111-1111-111111111111")
    with _ctx():
        await _service(redis, driver_a).semantic(graph_id="g1", query="q", top_k=10)
    with use_organisation_context(
        OrganisationContext(
            organisation_id=org_b, principal_id=org_b, principal_type=PrincipalType.USER
        )
    ):
        await _service(redis, driver_b).semantic(graph_id="g1", query="q", top_k=10)
    assert driver_a.calls == 1 and driver_b.calls == 1  # org-scoped key -> no cross-tenant hit


# --- ingest-time (generation) invalidation ------------------------------------


async def test_generation_bump_invalidates_the_cache() -> None:
    redis = _FakeAsyncRedis()
    driver = _CountingDriver()
    svc = _service(redis, driver)
    with _ctx():
        await svc.semantic(graph_id="g1", query="q", top_k=10)  # miss -> caches under generation 0
        await svc.semantic(graph_id="g1", query="q", top_k=10)  # hit
        assert driver.calls == 1
        # A new ingest bumps the per-graph generation (the KGS write-event) -> a natural miss.
        await redis.incr(graph_generation_key(str(_ORG), "g1"))
        await svc.semantic(graph_id="g1", query="q", top_k=10)
    assert driver.calls == 2


# --- enable-flag-off (None client = no caching) -------------------------------


async def test_cache_disabled_never_caches() -> None:
    driver = _CountingDriver()
    svc = _service(None, driver)  # cache disabled
    with _ctx():
        await svc.semantic(graph_id="g1", query="q", top_k=10)
        await svc.semantic(graph_id="g1", query="q", top_k=10)
        assert not svc._cache().enabled  # None client -> cache reports disabled
    assert driver.calls == 2  # every call re-hits the driver


# --- TTL ----------------------------------------------------------------------


async def test_set_applies_the_configured_ttl() -> None:
    redis = _FakeAsyncRedis()
    with _ctx():
        cache = QueryCacheRepository(redis, organisation_id=str(_ORG), ttl=42)
        await cache.set(
            graph_id="g1", query_text="q", retriever_type="semantic", result={"results": []}
        )
    # exactly one cached value key was written, and it carries the configured TTL
    cached_keys = [k for k in redis.ttls]
    assert len(cached_keys) == 1
    assert redis.ttls[cached_keys[0]] == 42


async def test_redis_error_degrades_to_a_miss_never_raises() -> None:
    class _BrokenRedis:
        async def get(self, key):
            raise RuntimeError("redis down")

        async def set(self, key, value, ex=None):
            raise RuntimeError("redis down")

    with _ctx():
        cache = QueryCacheRepository(_BrokenRedis(), organisation_id=str(_ORG))
        # both paths swallow the error: get -> None (a miss), set -> silently dropped
        assert await cache.get(graph_id="g1", query_text="q", retriever_type="semantic") is None
        await cache.set(
            graph_id="g1", query_text="q", retriever_type="semantic", result={"results": []}
        )
