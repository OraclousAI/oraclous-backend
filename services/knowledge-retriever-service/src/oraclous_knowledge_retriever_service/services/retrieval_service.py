"""Retrieval use-cases (ORAA-4 §21 services layer) — the five read modalities.

semantic (cosine over chunk embeddings, key-free), fulltext (index-free CONTAINS scan), hybrid (RRF
fusion, k=60), graph-traverse (1-hop neighbours), temporal (valid as-of). Every result is the
canonical `NodeResult` envelope {id, type, properties}; modality data (score, text, relationship,
…) lives inside properties (never at top level), and the embedding vector is never echoed. Org scope
is resolved from the bound context (fail-closed) and passed to the repository; sync Cypher runs off
the event loop via `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio

from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_retriever_service.contracts import EdgeResult, NodeResult, SubgraphResult
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
    ) -> None:
        self._driver = driver
        self._embedder = embedder
        self._db = database

    def _repo(self) -> RetrievalRepository:
        return RetrievalRepository(
            self._driver, organisation_id=enforced_organisation_id(), database=self._db
        )

    async def semantic(self, *, graph_id: str, query: str, top_k: int) -> list[NodeResult]:
        qvec = self._embedder.embed(query)
        repo = self._repo()
        rows = await asyncio.to_thread(repo.semantic, graph_id=graph_id, qvec=qvec, top_k=top_k)
        return [_to_node_result(r) for r in rows]

    async def fulltext(self, *, graph_id: str, query: str, top_k: int) -> list[NodeResult]:
        repo = self._repo()
        rows = await asyncio.to_thread(
            repo.fulltext,
            graph_id=graph_id,
            query=query,
            top_k=top_k,
        )
        return [_to_node_result(r) for r in rows]

    async def hybrid(self, *, graph_id: str, query: str, top_k: int) -> list[NodeResult]:
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
        return results

    async def neighbors(self, *, graph_id: str, node_id: str, top_k: int) -> list[NodeResult]:
        repo = self._repo()
        rows = await asyncio.to_thread(
            repo.neighbors, graph_id=graph_id, node_id=node_id, top_k=top_k
        )
        return [_to_node_result(r) for r in rows]

    async def temporal(self, *, graph_id: str, as_of: str, top_k: int) -> list[NodeResult]:
        repo = self._repo()
        rows = await asyncio.to_thread(repo.temporal, graph_id=graph_id, as_of=as_of, top_k=top_k)
        return [_to_node_result(r) for r in rows]

    async def subgraph(self, *, graph_id: str, limit: int) -> SubgraphResult:
        repo = self._repo()
        data = await asyncio.to_thread(repo.subgraph, graph_id=graph_id, limit=limit)
        return SubgraphResult(
            nodes=[_to_node_result(n) for n in data["nodes"]],
            edges=[_to_edge_result(e) for e in data["edges"]],
        )
