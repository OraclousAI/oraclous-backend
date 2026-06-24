"""Retrieval use-cases (services layer) — the five read modalities.

semantic (cosine over chunk embeddings, key-free), fulltext (index-free CONTAINS scan), hybrid (RRF
fusion, k=60), graph-traverse (1-hop neighbours), temporal (valid as-of). Every result is the
canonical `NodeResult` envelope {id, type, properties}; modality data (score, text, relationship,
…) lives inside properties (never at top level), and the embedding vector is never echoed. Org scope
is resolved from the bound context (fail-closed) and passed to the repository; sync Cypher runs off
the event loop via `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio

from oraclous_ohm.precedence_resolution import rank_hits_by_precedence
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_retriever_service.contracts import EdgeResult, NodeResult, SubgraphResult
from oraclous_knowledge_retriever_service.repositories.query_cache_repository import (
    QueryCacheRepository,
)
from oraclous_knowledge_retriever_service.repositories.retrieval_repository import (
    RetrievalRepository,
)
from oraclous_knowledge_retriever_service.services.embedder import HashingEmbedder

_RRF_K = 60


def _jsonable(value):
    """Coerce Neo4j-native values (e.g. neo4j.time.DateTime) to JSON-serialisable forms."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    for attr in ("isoformat", "iso_format"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 — fall through to str
                break
    return str(value)


def _to_node_result(row: dict) -> NodeResult:
    labels = [label for label in row.get("labels", []) if not str(label).startswith("__")]
    properties = {k: _jsonable(v) for k, v in row.get("props", {}).items() if k != "embedding"}
    if row.get("score") is not None:
        properties["score"] = row["score"]
    if row.get("relationship") is not None:
        properties["relationship"] = row["relationship"]
    return NodeResult(id=row["id"], type=labels[0] if labels else "Node", properties=properties)


def _apply_precedence(
    results: list[NodeResult], order: list[str] | None, *, graph_authoritative: bool
) -> list[NodeResult]:
    """Read-side Hierarchy-of-Truth ranking (#514, CTO ruling). When ``order`` is given, stamp each
    hit's path-derived ``precedence_tier`` (from its ``ingestion_source``) and DEMOTE lower-tier /
    derived hits below the canonical ones — stable, nothing dropped. No-op when ``order`` is empty.
    The tier/rank logic lives in ``oraclous_ohm`` (never in the service)."""
    if not order:
        return results
    ranked = rank_hits_by_precedence(
        results,
        lambda n: str(n["properties"].get("ingestion_source") or ""),
        order,
        graph_authoritative=graph_authoritative,
    )
    return [
        NodeResult(
            id=node["id"],
            type=node["type"],
            properties={**node["properties"], "precedence_tier": tier},
        )
        for node, tier in ranked
    ]


def _to_edge_result(row: dict) -> EdgeResult:
    # Mirror the node side: carry the edge property bag through (JSON-coerced) so edge-level
    # data — e.g. `score` on SIMILAR_TO/SAME_AS_CANDIDATE — reaches the FE explorer.
    properties = {k: _jsonable(v) for k, v in row.get("properties", {}).items()}
    return EdgeResult(
        source=row["source"], target=row["target"], type=row["type"], properties=properties
    )


class RetrievalService:
    def __init__(
        self,
        driver,
        embedder: HashingEmbedder,
        *,
        database: str | None = None,
        redis_client=None,
        cache_ttl: int = 300,
    ) -> None:
        self._driver = driver
        self._embedder = embedder
        self._db = database
        # Advisory query cache (#308): a None client (cache disabled / no Redis) makes the cache a
        # no-op, so the read path is identical with the flag off. Built per-request like _repo() so
        # the org scope comes from the same fail-closed governance seam.
        self._redis = redis_client
        self._cache_ttl = cache_ttl

    def _repo(self) -> RetrievalRepository:
        return RetrievalRepository(
            self._driver, organisation_id=enforced_organisation_id(), database=self._db
        )

    def _cache(self) -> QueryCacheRepository:
        return QueryCacheRepository(
            self._redis, organisation_id=enforced_organisation_id(), ttl=self._cache_ttl
        )

    @staticmethod
    def _cache_query(query: str, top_k: int) -> str:
        """Compose the cache-query string: the lower/stripped query (so case/whitespace variants
        collide, the legacy normalisation) plus top_k as a differentiator (a wider top_k is a
        distinct result set). The substrate key builder re-normalises, but normalising the query
        *before* appending top_k keeps the query's own trailing whitespace from leaking in."""
        return f"{query.lower().strip()}|top_k={top_k}"

    async def _cache_get(self, *, graph_id: str, query: str, modality: str):
        """Read a cached payload for (graph, modality, query), or None on miss/disabled."""
        return await self._cache().get(graph_id=graph_id, query_text=query, retriever_type=modality)

    async def _cache_set(self, *, graph_id: str, query: str, modality: str, payload: dict) -> None:
        """Cache `payload` for (graph, modality, query) under the current generation + TTL."""
        await self._cache().set(
            graph_id=graph_id, query_text=query, retriever_type=modality, result=payload
        )

    async def semantic(
        self,
        *,
        graph_id: str,
        query: str,
        top_k: int,
        precedence_order: list[str] | None = None,
        graph_authoritative: bool = False,
    ) -> list[NodeResult]:
        cache_query = self._cache_query(query, top_k)
        cached = await self._cache_get(graph_id=graph_id, query=cache_query, modality="semantic")
        if cached is not None:
            return _apply_precedence(
                cached["results"], precedence_order, graph_authoritative=graph_authoritative
            )
        qvec = self._embedder.embed(query)
        repo = self._repo()
        rows = await asyncio.to_thread(repo.semantic, graph_id=graph_id, qvec=qvec, top_k=top_k)
        results = [_to_node_result(r) for r in rows]
        await self._cache_set(
            graph_id=graph_id, query=cache_query, modality="semantic", payload={"results": results}
        )
        return _apply_precedence(results, precedence_order, graph_authoritative=graph_authoritative)

    async def fulltext(
        self,
        *,
        graph_id: str,
        query: str,
        top_k: int,
        precedence_order: list[str] | None = None,
        graph_authoritative: bool = False,
    ) -> list[NodeResult]:
        cache_query = self._cache_query(query, top_k)
        cached = await self._cache_get(graph_id=graph_id, query=cache_query, modality="fulltext")
        if cached is not None:
            return _apply_precedence(
                cached["results"], precedence_order, graph_authoritative=graph_authoritative
            )
        repo = self._repo()
        rows = await asyncio.to_thread(
            repo.fulltext,
            graph_id=graph_id,
            query=query,
            top_k=top_k,
        )
        results = [_to_node_result(r) for r in rows]
        await self._cache_set(
            graph_id=graph_id, query=cache_query, modality="fulltext", payload={"results": results}
        )
        return _apply_precedence(results, precedence_order, graph_authoritative=graph_authoritative)

    async def hybrid(
        self,
        *,
        graph_id: str,
        query: str,
        top_k: int,
        precedence_order: list[str] | None = None,
        graph_authoritative: bool = False,
    ) -> list[NodeResult]:
        cache_query = self._cache_query(query, top_k)
        cached = await self._cache_get(graph_id=graph_id, query=cache_query, modality="hybrid")
        if cached is not None:
            return _apply_precedence(
                cached["results"], precedence_order, graph_authoritative=graph_authoritative
            )
        # the fusion inputs stay UNRANKED (no precedence) — precedence applies to the fused result
        sem = await self.semantic(graph_id=graph_id, query=query, top_k=top_k * 2)
        ful = await self.fulltext(graph_id=graph_id, query=query, top_k=top_k * 2)
        fused: dict[str, dict] = {}
        for ranked in (sem, ful):
            for rank, node in enumerate(ranked, start=1):
                entry = fused.setdefault(node["id"], {"rrf": 0.0, "node": node})
                entry["rrf"] += 1.0 / (_RRF_K + rank)
        ordered = sorted(fused.values(), key=lambda e: e["rrf"], reverse=True)[:top_k]
        results: list[NodeResult] = []
        for entry in ordered:
            node = entry["node"]
            results.append(
                NodeResult(
                    id=node["id"],
                    type=node["type"],
                    properties={**node["properties"], "rrf_score": entry["rrf"]},
                )
            )
        await self._cache_set(
            graph_id=graph_id, query=cache_query, modality="hybrid", payload={"results": results}
        )
        return _apply_precedence(results, precedence_order, graph_authoritative=graph_authoritative)

    async def neighbors(self, *, graph_id: str, node_id: str, top_k: int) -> list[NodeResult]:
        repo = self._repo()
        rows = await asyncio.to_thread(
            repo.neighbors, graph_id=graph_id, node_id=node_id, top_k=top_k
        )
        return [_to_node_result(r) for r in rows]

    async def similar(
        self, *, graph_id: str, node_id: str, top_k: int, min_score: float
    ) -> list[NodeResult]:
        # find_similar (#310): the SIMILAR_TO neighbours of a node, ranked by the stamped cosine.
        # Each result carries `score` (the edge cosine) and `relationship` ("SIMILAR_TO") inside
        # `properties`, mirroring the other modalities; the embedding vector is never echoed.
        repo = self._repo()
        rows = await asyncio.to_thread(
            repo.similar, graph_id=graph_id, node_id=node_id, top_k=top_k, min_score=min_score
        )
        return [_to_node_result(r) for r in rows]

    async def graph_exists(self, *, graph_id: str) -> bool:
        """Org-scoped existence probe (#331): True iff the bound org has any node in `graph_id`."""
        repo = self._repo()
        return await asyncio.to_thread(repo.graph_exists, graph_id=graph_id)

    async def temporal(self, *, graph_id: str, as_of: str, top_k: int) -> list[NodeResult]:
        repo = self._repo()
        rows = await asyncio.to_thread(repo.temporal, graph_id=graph_id, as_of=as_of, top_k=top_k)
        return [_to_node_result(r) for r in rows]

    async def subgraph(self, *, graph_id: str, limit: int) -> SubgraphResult:
        cache_query = f"subgraph|limit={limit}"
        cached = await self._cache_get(graph_id=graph_id, query=cache_query, modality="subgraph")
        if cached is not None:
            return cached["result"]
        repo = self._repo()
        data = await asyncio.to_thread(repo.subgraph, graph_id=graph_id, limit=limit)
        result = SubgraphResult(
            nodes=[_to_node_result(n) for n in data["nodes"]],
            edges=[_to_edge_result(e) for e in data["edges"]],
        )
        await self._cache_set(
            graph_id=graph_id, query=cache_query, modality="subgraph", payload={"result": result}
        )
        return result
