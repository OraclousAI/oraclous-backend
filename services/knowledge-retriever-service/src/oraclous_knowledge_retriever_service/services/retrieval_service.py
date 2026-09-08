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

from oraclous_citation import Citation, citation_from_properties
from oraclous_citation.graph_properties import is_citation_property
from oraclous_ohm.precedence_resolution import rank_hits_by_precedence
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_retriever_service.contracts import EdgeResult, NodeResult, SubgraphResult
from oraclous_knowledge_retriever_service.repositories.query_cache_repository import (
    QueryCacheRepository,
)
from oraclous_knowledge_retriever_service.repositories.retrieval_repository import (
    LEGACY_NULL_EMBEDDER_ID,
    RetrievalRepository,
)
from oraclous_knowledge_retriever_service.services.embedder import Embedder

_RRF_K = 60


class EmbedderIdentityMismatch(Exception):
    """The graph holds chunks, but none under the query embedder's identity (#949 Q3).

    Fail-closed (§3.5): a graph mid-re-embed, or one written by a different model, holds vectors in
    a space this query embedder cannot be compared against. That is the core #643 failure mode — a
    plausible-looking cosine computed across two unrelated spaces — so it surfaces as a refusal the
    caller can act on, never as a silently empty result indistinguishable from a genuine miss.
    """


#: The one curated sentence a caller reads when the refusal above reaches HTTP. Spelled once, here
#: beside the exception it explains, because BOTH read surfaces raise it — single-graph search and
#: the federated fan-out — and two route modules each holding their own copy is the same
#: duplicated-spelling drift this whole change exists to remove. It names no graph id and no
#: identity string: the gateway drains an upstream body anyway, and a curated line is what a retry
#: would be based on.
IDENTITY_MISMATCH_DETAIL = (
    "this graph's stored embeddings were produced by a different embedder than this search uses,"
    " so they cannot be compared; re-embedding is in progress — try again shortly."
)


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
    stored = row.get("props", {})
    labels = [label for label in row.get("labels", []) if not str(label).startswith("__")]
    # The citation is LIFTED out of the property bag into a typed sibling (§CITE): the writer
    # flattens it onto the node because a Neo4j property must be a primitive, and the reader is
    # the other half of that pair. Nothing citation-shaped is left underneath — the console reads
    # one typed field, and a caller can never tell a planted key from a stamped one.
    citation = citation_from_properties(stored)
    properties = {
        k: _jsonable(v)
        for k, v in stored.items()
        if k != "embedding" and not is_citation_property(k)
    }
    if row.get("score") is not None:
        properties["score"] = row["score"]
    if row.get("relationship") is not None:
        properties["relationship"] = row["relationship"]
    return NodeResult(
        id=row["id"],
        type=labels[0] if labels else "Node",
        properties=properties,
        citation=citation,
    )


def _cacheable(results: list[NodeResult]) -> list[dict]:
    """The JSON-safe form of a result list, for the Redis query cache.

    ``Citation`` is a Pydantic model, and the cache serialises with ``json.dumps(..., default=str)``
    — which would store its repr and hand back an unparseable string on the next hit. Dump it
    properly here, and rehydrate on read, so a cached response is identical to a live one.
    """
    return [{**result, "citation": _dumped_citation(result.get("citation"))} for result in results]


def _dumped_citation(citation: Citation | None) -> dict | None:
    return citation.model_dump(mode="json") if citation is not None else None


def _from_cache(payload: list[dict]) -> list[NodeResult]:
    """Rehydrate ``_cacheable``'s output, so a cache hit returns the same typed envelope."""
    return [
        NodeResult(
            id=entry["id"],
            type=entry["type"],
            properties=entry["properties"],
            citation=(
                Citation.model_validate(entry["citation"])
                if entry.get("citation") is not None
                else None
            ),
        )
        for entry in payload
    ]


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
            citation=node["citation"],
        )
        for node, tier in ranked
    ]


def _degraded(node: NodeResult) -> NodeResult:
    """A combined-mode hit that was NOT fused, marked as such (#950 Q3).

    The mode has to be visible or the degradation is an invisible behaviour change. It rides in
    ``properties`` beside ``score``/``rrf_score``/``precedence_tier`` rather than as a sibling
    response field: the route's response model is a bare list, so a new envelope would be a
    cross-repo shape (§12) for something the existing free dict already carries. Deliberately no
    ``rrf_score`` — there was no fusion, and a score claiming otherwise would be a lie.
    """
    return NodeResult(
        id=node["id"],
        type=node["type"],
        properties={**node["properties"], "fusion_mode": "semantic_only"},
        citation=node["citation"],
    )


def _fused(node: NodeResult, rrf: float) -> NodeResult:
    """A genuinely fused combined-mode hit: its RRF score, and the mode that produced it."""
    return NodeResult(
        id=node["id"],
        type=node["type"],
        properties={**node["properties"], "rrf_score": rrf, "fusion_mode": "rrf"},
        citation=node["citation"],
    )


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
        embedder: Embedder,
        *,
        embedder_id: str = LEGACY_NULL_EMBEDDER_ID,
        database: str | None = None,
        redis_client=None,
        cache_ttl: int = 300,
    ) -> None:
        self._driver = driver
        self._embedder = embedder
        # #949 Q3: the identity of the space `embedder` produces vectors in — the ONE shared
        # `embedder_identity` string, the same function the write side stamps onto every :Chunk.
        # Never derived here from the embedder object, or the two sides could spell it differently
        # again, which is the whole of #643.
        self._embedder_id = embedder_id
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
                _from_cache(cached["results"]),
                precedence_order,
                graph_authoritative=graph_authoritative,
            )
        # The shared seam (`packages/embedding`) is batch-shaped — `embed(texts) -> vectors`.
        # One query is a one-element batch; the write side has always called it this way.
        (qvec,) = self._embedder.embed([query])
        repo = self._repo()
        rows = await asyncio.to_thread(
            repo.semantic,
            graph_id=graph_id,
            qvec=qvec,
            top_k=top_k,
            embedder_id=self._embedder_id,
        )
        if not rows and await asyncio.to_thread(repo.has_any_chunk, graph_id=graph_id):
            # The graph holds chunks, but the identity filter matched none of them: every vector in
            # it lives in a space this query cannot be compared against. Refuse. Returning [] here
            # would be indistinguishable from a genuine miss, and a caller cannot act on that — the
            # probe exists only to tell those two apart, and only runs when the filter came back
            # empty, so an ordinary search pays nothing for it.
            raise EmbedderIdentityMismatch(
                f"graph {graph_id} holds no chunks embedded by {self._embedder_id}"
            )
        results = [_to_node_result(r) for r in rows]
        await self._cache_set(
            graph_id=graph_id,
            query=cache_query,
            modality="semantic",
            payload={"results": _cacheable(results)},
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
                _from_cache(cached["results"]),
                precedence_order,
                graph_authoritative=graph_authoritative,
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
            graph_id=graph_id,
            query=cache_query,
            modality="fulltext",
            payload={"results": _cacheable(results)},
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
                _from_cache(cached["results"]),
                precedence_order,
                graph_authoritative=graph_authoritative,
            )
        # the fusion inputs stay UNRANKED (no precedence) — precedence applies to the fused result
        sem = await self.semantic(graph_id=graph_id, query=query, top_k=top_k * 2)
        word_search = await asyncio.to_thread(
            self._repo().fulltext_ranked, graph_id=graph_id, query=query, top_k=top_k * 2
        )
        if not word_search.ranked:
            # #950 Q3: RRF only means something when BOTH inputs are genuinely ranked. Today's
            # word search returns a constant score in storage order, so fusing it in does not add
            # relevance — it dilutes the one ranking that has any. Now that meaning-based search
            # is real, that would make combined mode look worse than semantic alone, and the new
            # embedder would get the blame. Degrade to the semantic list rather than refuse: a
            # caller asking for the best results still gets the best available ones.
            results = [_degraded(node) for node in sem[:top_k]]
        else:
            fused: dict[str, dict] = {}
            for ranked in (sem, [_to_node_result(r) for r in word_search.rows]):
                for rank, node in enumerate(ranked, start=1):
                    entry = fused.setdefault(node["id"], {"rrf": 0.0, "node": node})
                    entry["rrf"] += 1.0 / (_RRF_K + rank)
            ordered = sorted(fused.values(), key=lambda e: e["rrf"], reverse=True)[:top_k]
            results = [_fused(entry["node"], entry["rrf"]) for entry in ordered]
        await self._cache_set(
            graph_id=graph_id,
            query=cache_query,
            modality="hybrid",
            payload={"results": _cacheable(results)},
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
