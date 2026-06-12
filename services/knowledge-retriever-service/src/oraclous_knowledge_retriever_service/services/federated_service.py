"""Federated cross-graph retrieval (ORAA-4 §21 services layer) — #330 / ADR-026.

Query ALL the graphs a caller can read from one place. Four modes: ``entity`` (case-insensitive
name/alias match over canonical entities — the legacy UNION-ALL fan-out, lifted as per-graph
scoped calls), ``semantic`` (ONE query embedding via the existing 512-dim embedder, brute-force
cosine per graph — the path the legacy left as a stub, built real), ``fulltext`` (index-free
CONTAINS per graph), ``hybrid`` (RRF fusion of semantic + fulltext, k=60 — the single-graph
recipe, federated). Plus a federated neighborhood fetch around matched entities.

The no-new-access invariant (ADR-026): federation aggregates EXACTLY the graphs the caller can
already read individually. The accessible set is enumerated from the KGS registry over the
internal plane (``GraphRegistryClient``, org-scoped); every fan-out branch then ALSO binds the
org + graph predicates in-query (the same fail-closed repository the single-graph reads use) —
defence in depth, so even a poisoned graph list cannot read another org's nodes. An explicit
``graph_ids`` subset is validated ∩ accessible and FAIL-CLOSED: any unknown or inaccessible id
rejects the whole query (403, no partial results, no exists/doesn't-exist oracle). Caps (config):
max graphs per query, per-graph k, total merged cap.

The #308 query cache is deliberately NOT integrated here: its key folds a single graph's
generation counter, which cannot represent a multi-graph result's freshness — a federated read is
always live. Follow-up: a multi-graph generation vector key (noted on #330).

Every result is labeled ``source_graph_id`` + ``source_graph_name``. Embedder failure degrades
cleanly: semantic contributes nothing (flagged in meta), fulltext/entity still work.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_retriever_service.contracts import (
    FederatedEdgeResult,
    FederatedNodeResult,
    FederatedSubgraphResult,
)
from oraclous_knowledge_retriever_service.repositories.retrieval_repository import (
    RetrievalRepository,
)
from oraclous_knowledge_retriever_service.services.embedder import HashingEmbedder
from oraclous_knowledge_retriever_service.services.graph_registry_client import (
    GraphInfo,
    GraphRegistryClient,
)
from oraclous_knowledge_retriever_service.services.retrieval_service import _to_node_result

_RRF_K = 60

SEARCH_MODES = ("entity", "semantic", "fulltext", "hybrid")


class FederatedAccessError(Exception):
    """An explicit graph_ids subset names a graph the caller cannot read (or that does not
    exist — indistinguishable on purpose, no oracle). Fail-closed: the WHOLE query is rejected
    (403), never partial results."""


class FederatedCapError(Exception):
    """A requested fan-out exceeds a configured cap (max graphs / per-graph k / total). 422."""


class FederatedScope:
    """The resolved fan-out scope: the graphs to query + the ids skipped by the max-graphs cap."""

    def __init__(self, graphs: list[GraphInfo], skipped: list[str]) -> None:
        self.graphs = graphs
        self.skipped = skipped


def _labeled_node(row: dict, graph: GraphInfo) -> FederatedNodeResult:
    node = _to_node_result(row)
    return FederatedNodeResult(
        id=node["id"],
        type=node["type"],
        properties=node["properties"],
        source_graph_id=graph.id,
        source_graph_name=graph.name,
    )


def _labeled_edge(edge: dict, graph: GraphInfo) -> FederatedEdgeResult:
    properties = {k: v for k, v in (edge.get("properties") or {}).items()}
    return FederatedEdgeResult(
        source=edge["source"],
        target=edge["target"],
        type=edge["type"],
        properties=properties,
        source_graph_id=graph.id,
        source_graph_name=graph.name,
    )


def merge_ranked(
    per_graph: list[tuple[GraphInfo, list[dict]]], *, total_cap: int
) -> list[FederatedNodeResult]:
    """Merge per-graph rows into one ranked list: score desc, then (graph id, node id) for a
    deterministic order among ties — then apply the total cap."""
    labeled: list[FederatedNodeResult] = []
    for graph, rows in per_graph:
        labeled.extend(_labeled_node(row, graph) for row in rows)
    labeled.sort(
        key=lambda n: (-float(n["properties"].get("score", 0.0)), n["source_graph_id"], n["id"])
    )
    return labeled[:total_cap]


def rrf_fuse(
    ranked_lists: list[list[FederatedNodeResult]], *, total_cap: int
) -> list[FederatedNodeResult]:
    """Reciprocal-rank fusion (k=60, the single-graph hybrid recipe) across already-merged ranked
    lists, keyed by (source graph, node id) so the same name in two graphs never collapses."""
    fused: dict[tuple[str, str], dict] = {}
    for ranked in ranked_lists:
        for rank, node in enumerate(ranked, start=1):
            entry = fused.setdefault(
                (node["source_graph_id"], node["id"]), {"rrf": 0.0, "node": node}
            )
            entry["rrf"] += 1.0 / (_RRF_K + rank)
    ordered = sorted(
        fused.values(),
        key=lambda e: (-e["rrf"], e["node"]["source_graph_id"], e["node"]["id"]),
    )[:total_cap]
    results: list[FederatedNodeResult] = []
    for entry in ordered:
        node = entry["node"]
        results.append(
            FederatedNodeResult(
                id=node["id"],
                type=node["type"],
                properties={**node["properties"], "rrf_score": entry["rrf"]},
                source_graph_id=node["source_graph_id"],
                source_graph_name=node["source_graph_name"],
            )
        )
    return results


class FederatedRetrievalService:
    def __init__(
        self,
        driver,
        embedder: HashingEmbedder,
        registry: GraphRegistryClient,
        *,
        database: str | None = None,
        max_graphs: int,
        max_per_graph_k: int,
        max_total: int,
    ) -> None:
        self._driver = driver
        self._embedder = embedder
        self._registry = registry
        self._db = database
        self._max_graphs = max_graphs
        self._max_per_graph_k = max_per_graph_k
        self._max_total = max_total

    def _repo(self) -> RetrievalRepository:
        # The SAME fail-closed, org-scoped repository the single-graph reads use — every fan-out
        # branch carries the org + graph predicates as bound parameters.
        return RetrievalRepository(
            self._driver, organisation_id=enforced_organisation_id(), database=self._db
        )

    # ── scope resolution (the no-new-access gate) ────────────────────────────────────────────

    def check_caps(self, *, per_graph_k: int, total_k: int, graph_ids: list | None) -> None:
        if per_graph_k > self._max_per_graph_k:
            raise FederatedCapError(
                f"per_graph_k exceeds the configured cap ({self._max_per_graph_k})"
            )
        if total_k > self._max_total:
            raise FederatedCapError(f"total_k exceeds the configured cap ({self._max_total})")
        if graph_ids is not None and len(graph_ids) > self._max_graphs:
            raise FederatedCapError(
                f"too many graphs requested: max {self._max_graphs}, got {len(graph_ids)}"
            )

    async def resolve_scope(
        self, *, principal, graph_ids: list[uuid.UUID] | None
    ) -> FederatedScope:
        """Resolve the fan-out scope. Explicit subset = validated ∩ accessible, FAIL-CLOSED (any
        unknown/inaccessible id rejects the whole query). Default = ALL accessible graphs, capped
        at max_graphs (newest first; the rest reported as skipped, never silently dropped)."""
        accessible = await self._registry.accessible_graphs(principal)
        by_id = {g.id: g for g in accessible}
        if graph_ids:
            requested = list(dict.fromkeys(str(g) for g in graph_ids))  # dedup, order-preserving
            inaccessible = [g for g in requested if g not in by_id]
            if inaccessible:
                # One message for unknown AND other-org ids — no existence oracle.
                raise FederatedAccessError(
                    "one or more requested graphs are not accessible to the caller"
                )
            return FederatedScope([by_id[g] for g in requested], [])
        selected = accessible[: self._max_graphs]
        skipped = [g.id for g in accessible[self._max_graphs :]]
        return FederatedScope(selected, skipped)

    # ── fan-out ──────────────────────────────────────────────────────────────────────────────

    async def _fan_out(
        self,
        graphs: list[GraphInfo],
        call: Callable[[RetrievalRepository, str], Any],
    ) -> list[tuple[GraphInfo, Any]]:
        """Run one org+graph-scoped repository call per graph, concurrently (each branch is the
        same sync Cypher the single-graph reads issue, off the event loop)."""
        repo = self._repo()

        async def one(graph: GraphInfo) -> tuple[GraphInfo, Any]:
            return graph, await asyncio.to_thread(call, repo, graph.id)

        return list(await asyncio.gather(*(one(g) for g in graphs)))

    def _try_embed(self, query: str) -> list[float] | None:
        """The ONE query embedding shared by every semantic branch (existing 512-dim embedder
        config). None on failure — semantic degrades cleanly instead of failing the query."""
        try:
            qvec = self._embedder.embed(query)
        except Exception:  # noqa: BLE001 — degrade-don't-crash: fulltext/entity still serve.
            return None
        if not qvec or all(v == 0.0 for v in qvec):
            return None  # nothing to compare against (e.g. an all-unknown-token query)
        return qvec

    # ── public API ───────────────────────────────────────────────────────────────────────────

    async def search(
        self,
        *,
        principal,
        query: str,
        mode: str,
        graph_ids: list[uuid.UUID] | None,
        per_graph_k: int,
        total_k: int,
    ) -> dict:
        """Federated search in one of the four modes. Returns
        ``{results, meta: {graphs_queried, graphs_skipped, mode, semantic_degraded}}``."""
        self.check_caps(per_graph_k=per_graph_k, total_k=total_k, graph_ids=graph_ids)
        scope = await self.resolve_scope(principal=principal, graph_ids=graph_ids)
        semantic_degraded = False

        if mode == "entity":
            per_graph = await self._fan_out(
                scope.graphs,
                lambda repo, gid: repo.entity_search(graph_id=gid, term=query, top_k=per_graph_k),
            )
            results = merge_ranked(per_graph, total_cap=total_k)
        elif mode == "fulltext":
            per_graph = await self._fan_out(
                scope.graphs,
                lambda repo, gid: repo.fulltext(graph_id=gid, query=query, top_k=per_graph_k),
            )
            results = merge_ranked(per_graph, total_cap=total_k)
        elif mode == "semantic":
            qvec = self._try_embed(query)
            if qvec is None:
                semantic_degraded = True
                results = []
            else:
                per_graph = await self._fan_out(
                    scope.graphs,
                    lambda repo, gid: repo.semantic(graph_id=gid, qvec=qvec, top_k=per_graph_k),
                )
                results = merge_ranked(per_graph, total_cap=total_k)
        elif mode == "hybrid":
            qvec = self._try_embed(query)
            sem: list[FederatedNodeResult] = []
            if qvec is None:
                semantic_degraded = True
            else:
                sem_rows = await self._fan_out(
                    scope.graphs,
                    lambda repo, gid: repo.semantic(graph_id=gid, qvec=qvec, top_k=per_graph_k),
                )
                sem = merge_ranked(sem_rows, total_cap=self._max_total)
            ful_rows = await self._fan_out(
                scope.graphs,
                lambda repo, gid: repo.fulltext(graph_id=gid, query=query, top_k=per_graph_k),
            )
            ful = merge_ranked(ful_rows, total_cap=self._max_total)
            results = rrf_fuse([sem, ful], total_cap=total_k)
        else:  # pragma: no cover — the schema's Literal already rejects unknown modes.
            raise FederatedCapError(f"unknown mode {mode!r}")

        return {
            "results": results,
            "meta": {
                "graphs_queried": [{"id": g.id, "name": g.name} for g in scope.graphs],
                "graphs_skipped": scope.skipped,
                "mode": mode,
                "semantic_degraded": semantic_degraded,
            },
        }

    async def neighborhood(
        self,
        *,
        principal,
        query: str,
        graph_ids: list[uuid.UUID] | None,
        entities_per_graph: int,
        limit_per_graph: int,
    ) -> dict:
        """Federated subgraph fetch: match entities per graph, then return the 1-hop neighborhood
        slice around them — every node and edge labeled with its source graph. Edges never cross
        graphs (each branch is org+graph scoped on both endpoints)."""
        self.check_caps(
            per_graph_k=entities_per_graph, total_k=limit_per_graph, graph_ids=graph_ids
        )
        scope = await self.resolve_scope(principal=principal, graph_ids=graph_ids)

        matched = await self._fan_out(
            scope.graphs,
            lambda repo, gid: repo.entity_search(
                graph_id=gid, term=query, top_k=entities_per_graph
            ),
        )

        repo = self._repo()

        async def one(graph: GraphInfo, node_ids: list[str]) -> tuple[GraphInfo, dict]:
            if not node_ids:
                return graph, {"nodes": [], "edges": []}
            return graph, await asyncio.to_thread(
                repo.entity_neighborhood,
                graph_id=graph.id,
                node_ids=node_ids,
                limit=limit_per_graph,
            )

        slices = await asyncio.gather(
            *(one(graph, [row["id"] for row in rows]) for graph, rows in matched)
        )

        subgraph = FederatedSubgraphResult(nodes=[], edges=[])
        for graph, data in slices:
            subgraph["nodes"].extend(_labeled_node(n, graph) for n in data["nodes"])
            subgraph["edges"].extend(_labeled_edge(e, graph) for e in data["edges"])
        return {
            "nodes": subgraph["nodes"],
            "edges": subgraph["edges"],
            "meta": {
                "graphs_queried": [{"id": g.id, "name": g.name} for g in scope.graphs],
                "graphs_skipped": scope.skipped,
                "mode": "entity",
                "semantic_degraded": False,
            },
        }
