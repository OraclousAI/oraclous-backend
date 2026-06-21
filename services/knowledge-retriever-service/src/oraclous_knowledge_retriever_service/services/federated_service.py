"""Federated cross-graph retrieval (services layer) — #330 / ADR-026.

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

from oraclous_substrate.access import (
    CrossOrganisationDenied,
    authorise_cross_org_traversal,
    enforced_organisation_id,
)
from oraclous_substrate.rebac import AccessDecisionClient

from oraclous_knowledge_retriever_service.contracts import (
    FederatedEdgeResult,
    FederatedNodeResult,
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


def merge_score_desc(
    per_graph: list[tuple[GraphInfo, list[dict]]], *, total_cap: int
) -> list[FederatedNodeResult]:
    """Merge GENUINELY score-bearing per-graph rows (semantic) into one ranked list: score desc,
    then (graph id, node id) for a deterministic order among ties — then apply the total cap. Only
    for modes whose per-graph score actually discriminates; constant-score modes use the fair
    round-robin interleave below instead (a global score-sort there degenerates to graph-UUID
    order, starving the trailing graphs)."""
    labeled: list[FederatedNodeResult] = []
    for graph, rows in per_graph:
        labeled.extend(_labeled_node(row, graph) for row in rows)
    labeled.sort(
        key=lambda n: (-float(n["properties"].get("score", 0.0)), n["source_graph_id"], n["id"])
    )
    return labeled[:total_cap]


def interleave_round_robin(
    per_graph: list[tuple[GraphInfo, list[dict]]], *, total_cap: int
) -> list[FederatedNodeResult]:
    """Fair cross-graph merge for CONSTANT-score modes (entity, fulltext): take rank-1 from each
    graph, then rank-2, … up to the total cap — preserving each graph's own internal order. The
    total cap then draws fairly across graphs rather than filling from the first graphs by UUID
    (which a global score-desc sort degenerates to when every score is 1.0)."""
    if total_cap <= 0:
        # The cap is checked AFTER each append below, so a non-positive cap would otherwise emit
        # one element before tripping. Guard up front: a zero/negative cap selects nothing.
        return []
    columns = [[_labeled_node(row, graph) for row in rows] for graph, rows in per_graph]
    results: list[FederatedNodeResult] = []
    depth = max((len(col) for col in columns), default=0)
    for rank in range(depth):
        for col in columns:
            if rank < len(col):
                results.append(col[rank])
                if len(results) >= total_cap:
                    return results
    return results


def rrf_fuse(
    ranked_lists: list[list[list[FederatedNodeResult]]], *, total_cap: int
) -> list[FederatedNodeResult]:
    """Reciprocal-rank fusion (k=60, the single-graph hybrid recipe) across constituent rankings,
    keyed by (source graph, node id) so the same name in two graphs never collapses. Each input is
    a list of PER-GRAPH ranked lists; a node's RRF rank is its LOCAL position within its own
    graph's list — so fusion is independent of cross-graph merge order (graph-UUID order can never
    bias it)."""
    fused: dict[tuple[str, str], dict] = {}
    for per_graph_lists in ranked_lists:
        for graph_ranked in per_graph_lists:
            for rank, node in enumerate(graph_ranked, start=1):
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
        max_subgraph_nodes: int,
        rebac_client: AccessDecisionClient | None = None,
    ) -> None:
        self._driver = driver
        self._embedder = embedder
        self._registry = registry
        self._db = database
        # The fail-closed cross-org access-decision client (#446). None = cross-org admission OFF:
        # an explicitly-named foreign graph stays inaccessible (the pre-#446 behaviour), never
        # fail-open. When present, a granted foreign graph is ADMITTED into the fan-out scope.
        self._rebac = rebac_client
        self._max_graphs = max_graphs
        self._max_per_graph_k = max_per_graph_k
        self._max_total = max_total
        # A single CROSS-GRAPH ceiling on the merged subgraph node count — applied to the union of
        # every graph's slice, NOT max_graphs × per-graph (which would let a default-all fetch
        # return thousands of nodes). The aggregate cap the FE explorer is sized for.
        self._max_subgraph_nodes = max_subgraph_nodes

    def _repo(self, organisation_id: str | None = None) -> RetrievalRepository:
        # The SAME fail-closed, org-scoped repository the single-graph reads use — every fan-out
        # branch carries the org + graph predicates as bound parameters.
        #
        # Per-branch org binding (ADR-036): a cross-org graph admitted by a ReBAC grant passes its
        # OWNER org here, so THAT branch reads the owner's rows; home/default graphs pass None → the
        # caller's enforced org (unchanged). The graph_id predicate still pins the slice, so a
        # granted branch can only ever read that one graph's owner-org rows, never another.
        return RetrievalRepository(
            self._driver,
            organisation_id=organisation_id or enforced_organisation_id(),
            database=self._db,
        )

    # ── scope resolution (the no-new-access gate) ────────────────────────────────────────────

    def _check_graphs_cap(self, graph_ids: list | None) -> None:
        if graph_ids is not None and len(graph_ids) > self._max_graphs:
            raise FederatedCapError(
                f"too many graphs requested: max {self._max_graphs}, got {len(graph_ids)}"
            )

    def check_caps(self, *, per_graph_k: int, total_k: int, graph_ids: list | None) -> None:
        if per_graph_k > self._max_per_graph_k:
            raise FederatedCapError(
                f"per_graph_k exceeds the configured cap ({self._max_per_graph_k})"
            )
        if total_k > self._max_total:
            raise FederatedCapError(f"total_k exceeds the configured cap ({self._max_total})")
        self._check_graphs_cap(graph_ids)

    def check_subgraph_caps(
        self, *, entities_per_graph: int, limit_per_graph: int, graph_ids: list | None
    ) -> None:
        # Cap-breach messages name the ACTUAL subgraph request fields (the subgraph body has no
        # `total_k`/`per_graph_k`). entities_per_graph + limit_per_graph each reuse the per-graph-k
        # ceiling; the merged result is bounded separately by
        # max_subgraph_nodes (the cross-graph aggregate cap, applied in _merge_subgraph).
        if entities_per_graph > self._max_per_graph_k:
            raise FederatedCapError(
                f"entities_per_graph exceeds the configured cap ({self._max_per_graph_k})"
            )
        if limit_per_graph > self._max_per_graph_k:
            raise FederatedCapError(
                f"limit_per_graph exceeds the configured cap ({self._max_per_graph_k})"
            )
        self._check_graphs_cap(graph_ids)

    async def resolve_scope(
        self, *, principal, graph_ids: list[uuid.UUID] | None
    ) -> FederatedScope:
        """Resolve the fan-out scope. Explicit subset = validated against (home-org accessible ∪
        ReBAC-granted), FAIL-CLOSED (any id that is neither rejects the whole query). Default = ALL
        home-org accessible graphs, capped at max_graphs (newest first; the rest reported as
        skipped, never silently dropped) — the default set is NEVER widened by cross-org grants."""
        accessible = await self._registry.accessible_graphs(principal)
        by_id = {g.id: g for g in accessible}
        if graph_ids is not None:
            # An EXPLICIT subset. None/omitted = all accessible; an explicit empty list is a caller
            # error (an empty selection selects nothing — never silently "all"), rejected as 422.
            if not graph_ids:
                raise FederatedCapError("graph_ids must not be an empty list (omit it for all)")
            requested = list(dict.fromkeys(str(g) for g in graph_ids))  # dedup, order-preserving
            inaccessible = [g for g in requested if g not in by_id]
            # #446: a graph outside the home-org set may still be reachable by an explicit ReBAC
            # grant (ADR-004). Admit the granted ones; the rest stay inaccessible (fail-closed).
            granted = await self._admit_granted(inaccessible) if inaccessible else {}
            still_denied = [g for g in inaccessible if g not in granted]
            if still_denied:
                # One message for unknown AND ungranted other-org ids — no existence oracle.
                raise FederatedAccessError(
                    "one or more requested graphs are not accessible to the caller"
                )
            return FederatedScope([by_id[g] if g in by_id else granted[g] for g in requested], [])
        selected = accessible[: self._max_graphs]
        skipped = [g.id for g in accessible[self._max_graphs :]]
        return FederatedScope(selected, skipped)

    async def _admit_granted(self, graph_ids: list[str]) -> dict[str, GraphInfo]:
        """ReBAC-check each foreign (non-home-org) graph the caller explicitly named. A graph the
        caller has been granted a read on (a HAS_ROLE relation, ADR-004) is ADMITTED into the
        fan-out as a minimal ``GraphInfo``; anything else stays denied. FAIL-CLOSED: with no client
        (admission OFF) or on ANY non-grant decision (absent / ambiguous / store error — all of
        which ``authorise_cross_org_traversal`` collapses to a denial), the graph is NOT admitted.
        The org + subject for the decision come from the bound org-context, never an argument
        (ADR-006)."""
        if self._rebac is None:
            return {}
        granted: dict[str, GraphInfo] = {}
        for gid in graph_ids:
            try:
                owner_org = await authorise_cross_org_traversal(
                    self._rebac, resource=f"graph-{gid}", relation="read"
                )
            except CrossOrganisationDenied:
                continue  # not granted → stays inaccessible (no existence oracle in the message)
            # ADR-036: admit ONLY if the grant carries an OWNER org (the org whose rows the read
            # binds). A grant without one (e.g. written before ADR-036) is DROPPED — never read
            # under the caller's org, never None into Cypher; it returns empty until re-granted.
            if not owner_org:
                continue
            # Admitted. The grantee org cannot see the foreign graph's name (it is the owner org's),
            # so the placeholder name stands; the owner org binds the per-branch read to exactly
            # this graph's owner-org rows (the graph_id predicate pins the slice — no widening).
            granted[gid] = GraphInfo(id=gid, name="(shared graph)", owner_organisation_id=owner_org)
        return granted

    # ── fan-out ──────────────────────────────────────────────────────────────────────────────

    async def _fan_out(
        self,
        graphs: list[GraphInfo],
        call: Callable[[RetrievalRepository, str], Any],
        *,
        failed: list[str],
    ) -> list[tuple[GraphInfo, Any]]:
        """Run one org+graph-scoped repository call per graph, concurrently (each branch is the
        same sync Cypher the single-graph reads issue, off the event loop). Partial-result: a
        branch that raises (e.g. one graph's Neo4j error) is DROPPED — its graph id is appended to
        ``failed`` and the successful graphs' results are returned, so one bad graph never 500s the
        whole federated query. The org/graph predicates are unaffected (security is per-branch and
        in-query), so dropping a branch cannot widen access."""

        async def one(graph: GraphInfo) -> Any:
            # ADR-036: per-branch repository — bound to the graph's OWNER org for an admitted
            # cross-org grant, else the caller's org. Built on the event loop (org context bound)
            # before the off-loop Cypher; never one shared repo (which would bleed an owner org
            # across concurrent branches).
            repo = self._repo(graph.owner_organisation_id)
            return await asyncio.to_thread(call, repo, graph.id)

        outcomes = await asyncio.gather(*(one(g) for g in graphs), return_exceptions=True)
        results: list[tuple[GraphInfo, Any]] = []
        for graph, outcome in zip(graphs, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failed.append(graph.id)
            else:
                results.append((graph, outcome))
        return results

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
        ``{results, meta: {graphs_queried, graphs_skipped, graphs_failed, mode,
        semantic_degraded}}``. Constant-score modes (entity, fulltext) merge by a fair
        round-robin interleave across graphs; semantic (genuine scores) merges score-desc; hybrid
        fuses the PER-GRAPH local rankings via RRF (graph-order-independent)."""
        self.check_caps(per_graph_k=per_graph_k, total_k=total_k, graph_ids=graph_ids)
        scope = await self.resolve_scope(principal=principal, graph_ids=graph_ids)
        semantic_degraded = False
        failed: list[str] = []

        if mode == "entity":
            per_graph = await self._fan_out(
                scope.graphs,
                lambda repo, gid: repo.entity_search(graph_id=gid, term=query, top_k=per_graph_k),
                failed=failed,
            )
            results = interleave_round_robin(per_graph, total_cap=total_k)
        elif mode == "fulltext":
            per_graph = await self._fan_out(
                scope.graphs,
                lambda repo, gid: repo.fulltext(graph_id=gid, query=query, top_k=per_graph_k),
                failed=failed,
            )
            results = interleave_round_robin(per_graph, total_cap=total_k)
        elif mode == "semantic":
            qvec = self._try_embed(query)
            if qvec is None:
                semantic_degraded = True
                results = []
            else:
                qvec_nn = qvec
                per_graph = await self._fan_out(
                    scope.graphs,
                    lambda repo, gid: repo.semantic(graph_id=gid, qvec=qvec_nn, top_k=per_graph_k),
                    failed=failed,
                )
                results = merge_score_desc(per_graph, total_cap=total_k)
        elif mode == "hybrid":
            qvec = self._try_embed(query)
            sem_lists: list[list[FederatedNodeResult]] = []
            # Per-BRANCH failure tracking: a hybrid graph is only "failed" if it contributed via
            # NEITHER branch. A graph whose semantic arm errored but whose fulltext arm succeeded
            # (or vice versa) still contributed to the fused result, so it must NOT be reported as
            # failed. We collect each branch's own failed list, then report only the intersection
            # of graphs that failed every branch they were attempted in.
            sem_failed: list[str] = []
            ful_failed: list[str] = []
            if qvec is None:
                semantic_degraded = True
            else:
                sem_rows = await self._fan_out(
                    scope.graphs,
                    lambda repo, gid: repo.semantic(graph_id=gid, qvec=qvec, top_k=per_graph_k),
                    failed=sem_failed,
                )
                sem_lists = self._per_graph_lists(sem_rows)
            ful_rows = await self._fan_out(
                scope.graphs,
                lambda repo, gid: repo.fulltext(graph_id=gid, query=query, top_k=per_graph_k),
                failed=ful_failed,
            )
            ful_lists = self._per_graph_lists(ful_rows)
            # RRF over each constituent's PER-GRAPH local rankings — fusion is graph-order
            # independent (a node's rank is its position within its own graph's list).
            results = rrf_fuse([sem_lists, ful_lists], total_cap=total_k)
            # A graph is hybrid-failed only if it errored on the fulltext branch AND (the semantic
            # branch was degraded-off OR it also errored on the semantic branch) — i.e. it produced
            # no contribution at all. (When semantic is off, fulltext is the sole branch, so its
            # failures stand alone.)
            if qvec is None:
                failed = list(ful_failed)
            else:
                sem_failed_set = set(sem_failed)
                failed = [g for g in ful_failed if g in sem_failed_set]
        else:  # pragma: no cover — the schema's Literal already rejects unknown modes.
            raise FederatedCapError(f"unknown mode {mode!r}")

        return {
            "results": results,
            "meta": self._meta(
                scope, mode=mode, semantic_degraded=semantic_degraded, failed=failed
            ),
        }

    @staticmethod
    def _per_graph_lists(
        per_graph: list[tuple[GraphInfo, list[dict]]],
    ) -> list[list[FederatedNodeResult]]:
        """Label each graph's rows in its OWN repository order — one ranked list per graph, for
        RRF's per-graph local-rank fusion (no cross-graph re-sort)."""
        return [[_labeled_node(row, graph) for row in rows] for graph, rows in per_graph]

    def _meta(
        self,
        scope: FederatedScope,
        *,
        mode: str,
        semantic_degraded: bool,
        failed: list[str],
    ) -> dict:
        return {
            "graphs_queried": [{"id": g.id, "name": g.name} for g in scope.graphs],
            "graphs_skipped": scope.skipped,
            # Graphs that contributed NO results because every branch they were attempted on errored
            # (partial result; mirrors the semantic_degraded clean-degrade pattern). For hybrid this
            # is the intersection of the per-branch failures — a graph that failed one branch but
            # served the other is NOT listed. Deduped, deterministic order.
            "graphs_failed": list(dict.fromkeys(failed)),
            "mode": mode,
            "semantic_degraded": semantic_degraded,
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
        graphs (each branch is org+graph scoped on both endpoints). A single AGGREGATE node cap
        (``max_subgraph_nodes``) bounds the merged result across ALL graphs, so a default-all fetch
        cannot return thousands of nodes; the cap is drawn fairly via a round-robin across graphs.
        Partial-result: a graph whose branch errors is dropped and reported in
        ``meta.graphs_failed``."""
        self.check_subgraph_caps(
            entities_per_graph=entities_per_graph,
            limit_per_graph=limit_per_graph,
            graph_ids=graph_ids,
        )
        scope = await self.resolve_scope(principal=principal, graph_ids=graph_ids)
        failed: list[str] = []

        matched = await self._fan_out(
            scope.graphs,
            lambda repo, gid: repo.entity_search(
                graph_id=gid, term=query, top_k=entities_per_graph
            ),
            failed=failed,
        )

        async def one(graph: GraphInfo, node_ids: list[str]) -> dict:
            if not node_ids:
                return {"nodes": [], "edges": []}
            # ADR-036: per-branch repo (owner org for an admitted grant, else the caller's), same as
            # _fan_out — the neighborhood read of a granted graph binds the owner org too.
            repo = self._repo(graph.owner_organisation_id)
            return await asyncio.to_thread(
                repo.entity_neighborhood,
                graph_id=graph.id,
                node_ids=node_ids,
                limit=limit_per_graph,
            )

        graphs_in_order = [graph for graph, _ in matched]
        outcomes = await asyncio.gather(
            *(one(graph, [row["id"] for row in rows]) for graph, rows in matched),
            return_exceptions=True,
        )
        per_graph_slices: list[tuple[GraphInfo, dict]] = []
        for graph, outcome in zip(graphs_in_order, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failed.append(graph.id)
            else:
                per_graph_slices.append((graph, outcome))

        nodes, edges = self._merge_subgraph(per_graph_slices)
        return {
            "nodes": nodes,
            "edges": edges,
            "meta": self._meta(scope, mode="entity", semantic_degraded=False, failed=failed),
        }

    def _merge_subgraph(
        self, per_graph_slices: list[tuple[GraphInfo, dict]]
    ) -> tuple[list[FederatedNodeResult], list[FederatedEdgeResult]]:
        """Merge each graph's slice under the cross-graph aggregate node cap. Nodes are drawn
        round-robin across graphs (each graph keeps its own slice order) so the ceiling never
        starves the trailing graphs; edges survive only when BOTH endpoints made the node cut (the
        invariant the FE relies on), keeping a stray edge from referencing a dropped node."""
        if self._max_subgraph_nodes <= 0:
            # The cap is checked AFTER each append below, so a non-positive cap would otherwise keep
            # one node (and any edge between two such). Guard up front: a zero cap keeps nothing.
            return [], []
        labeled_columns = [
            [(graph, _labeled_node(n, graph)) for n in data["nodes"]]
            for graph, data in per_graph_slices
        ]
        kept_nodes: list[FederatedNodeResult] = []
        kept_ids_by_graph: dict[str, set[str]] = {}
        depth = max((len(col) for col in labeled_columns), default=0)
        for rank in range(depth):
            for col in labeled_columns:
                if rank < len(col):
                    graph, node = col[rank]
                    kept_nodes.append(node)
                    kept_ids_by_graph.setdefault(graph.id, set()).add(node["id"])
                    if len(kept_nodes) >= self._max_subgraph_nodes:
                        return kept_nodes, self._edges_within(per_graph_slices, kept_ids_by_graph)
        return kept_nodes, self._edges_within(per_graph_slices, kept_ids_by_graph)

    @staticmethod
    def _edges_within(
        per_graph_slices: list[tuple[GraphInfo, dict]],
        kept_ids_by_graph: dict[str, set[str]],
    ) -> list[FederatedEdgeResult]:
        edges: list[FederatedEdgeResult] = []
        for graph, data in per_graph_slices:
            kept = kept_ids_by_graph.get(graph.id, set())
            for e in data["edges"]:
                if e["source"] in kept and e["target"] in kept:
                    edges.append(_labeled_edge(e, graph))
        return edges
