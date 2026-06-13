"""Unit: FederatedRetrievalService (#330 / ADR-026) — scope resolution, caps, fan-out merge,
labeling, degrade.

In isolation from Neo4j/HTTP: the repository is a fake recording per-graph calls (proving every
branch is graph-scoped), the registry client is a fake returning a fixed accessible set. The
governance-relevant behaviour: an explicit subset naming an inaccessible/unknown graph rejects the
WHOLE query (fail-closed, no partial results); caps reject above config; default-all truncates to
the max-graphs cap and reports the skipped ids; every result carries its source-graph label; a
failing embedder degrades semantic to empty while fulltext still serves.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_knowledge_retriever_service.services.federated_service import (
    FederatedAccessError,
    FederatedCapError,
    FederatedRetrievalService,
    interleave_round_robin,
    merge_score_desc,
    rrf_fuse,
)
from oraclous_knowledge_retriever_service.services.graph_registry_client import (
    GraphInfo,
    GraphRegistryError,
)

pytestmark = pytest.mark.unit

_G1 = GraphInfo(id="11111111-1111-1111-1111-111111111111", name="research")
_G2 = GraphInfo(id="22222222-2222-2222-2222-222222222222", name="sales")
_G3 = GraphInfo(id="33333333-3333-3333-3333-333333333333", name="ops")
_OTHER_ORG_GRAPH = uuid.UUID("44444444-4444-4444-4444-444444444444")


class _FakeRegistry:
    def __init__(self, graphs: list[GraphInfo], error: Exception | None = None) -> None:
        self._graphs = graphs
        self._error = error

    async def accessible_graphs(self, principal) -> list[GraphInfo]:
        if self._error is not None:
            raise self._error
        return list(self._graphs)


class _FakeRepo:
    """Records the (method, graph_id) of every branch; returns canned per-graph rows."""

    def __init__(self, rows_by_graph: dict[str, list[dict]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._rows = rows_by_graph or {}

    def _rows_for(self, method: str, graph_id: str) -> list[dict]:
        self.calls.append((method, graph_id))
        return list(self._rows.get(graph_id, []))

    def entity_search(self, *, graph_id, term, top_k):
        return self._rows_for("entity", graph_id)[:top_k]

    def fulltext(self, *, graph_id, query, top_k):
        return self._rows_for("fulltext", graph_id)[:top_k]

    def semantic(self, *, graph_id, qvec, top_k):
        return self._rows_for("semantic", graph_id)[:top_k]

    def entity_neighborhood(self, *, graph_id, node_ids, limit):
        self.calls.append(("neighborhood", graph_id))
        return {"nodes": [], "edges": []}


class _FailingEmbedder:
    dim = 512

    def embed(self, text: str) -> list[float]:
        raise RuntimeError("embedder is off")


class _OkEmbedder:
    dim = 4

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]


def _row(graph_id: str, ident: str, score: float = 1.0) -> dict:
    return {
        "id": ident,
        "labels": ["Chunk"],
        "props": {"name": ident, "graph_id": graph_id},
        "score": score,
    }


class _Service(FederatedRetrievalService):
    """Test double: bypass the governance-bound repo builder with the injected fake."""

    def __init__(self, repo, registry, embedder=None, **caps) -> None:
        super().__init__(
            driver=None,
            embedder=embedder or _OkEmbedder(),
            registry=registry,
            max_graphs=caps.get("max_graphs", 20),
            max_per_graph_k=caps.get("max_per_graph_k", 25),
            max_total=caps.get("max_total", 200),
            max_subgraph_nodes=caps.get("max_subgraph_nodes", 500),
        )
        self._fake_repo = repo

    def _repo(self):
        return self._fake_repo


def _svc(repo=None, graphs=None, **kw) -> _Service:
    return _Service(repo or _FakeRepo(), _FakeRegistry(graphs or [_G1, _G2, _G3]), **kw)


# ── scope resolution: the no-new-access gate ────────────────────────────────────────────────


async def test_default_scope_is_all_accessible_graphs() -> None:
    repo = _FakeRepo()
    out = await _svc(repo).search(
        principal=None, query="x", mode="entity", graph_ids=None, per_graph_k=5, total_k=50
    )
    assert [c[1] for c in repo.calls] == [_G1.id, _G2.id, _G3.id]
    assert [g["id"] for g in out["meta"]["graphs_queried"]] == [_G1.id, _G2.id, _G3.id]
    assert out["meta"]["graphs_skipped"] == []


async def test_explicit_subset_runs_only_those_graphs() -> None:
    repo = _FakeRepo()
    out = await _svc(repo).search(
        principal=None,
        query="x",
        mode="entity",
        graph_ids=[uuid.UUID(_G2.id)],
        per_graph_k=5,
        total_k=50,
    )
    assert [c[1] for c in repo.calls] == [_G2.id]
    assert out["meta"]["graphs_queried"] == [{"id": _G2.id, "name": "sales"}]


async def test_inaccessible_subset_id_fails_closed_with_no_partial_results() -> None:
    repo = _FakeRepo({_G1.id: [_row(_G1.id, "n1")]})
    with pytest.raises(FederatedAccessError):
        await _svc(repo).search(
            principal=None,
            query="x",
            mode="entity",
            graph_ids=[uuid.UUID(_G1.id), _OTHER_ORG_GRAPH],  # one good + one foreign
            per_graph_k=5,
            total_k=50,
        )
    assert repo.calls == []  # the WHOLE query was rejected before any branch ran


async def test_default_all_truncates_to_max_graphs_and_reports_skipped() -> None:
    repo = _FakeRepo()
    out = await _svc(repo, max_graphs=2).search(
        principal=None, query="x", mode="entity", graph_ids=None, per_graph_k=5, total_k=50
    )
    assert [c[1] for c in repo.calls] == [_G1.id, _G2.id]
    assert out["meta"]["graphs_skipped"] == [_G3.id]


async def test_registry_error_propagates_failing_closed() -> None:
    svc = _Service(_FakeRepo(), _FakeRegistry([], error=GraphRegistryError("down")))
    with pytest.raises(GraphRegistryError):
        await svc.search(
            principal=None, query="x", mode="entity", graph_ids=None, per_graph_k=5, total_k=50
        )


# ── caps ─────────────────────────────────────────────────────────────────────────────────────


async def test_caps_reject_above_config() -> None:
    svc = _svc(max_per_graph_k=10, max_total=20, max_graphs=2)
    with pytest.raises(FederatedCapError):
        await svc.search(
            principal=None, query="x", mode="entity", graph_ids=None, per_graph_k=11, total_k=20
        )
    with pytest.raises(FederatedCapError):
        await svc.search(
            principal=None, query="x", mode="entity", graph_ids=None, per_graph_k=5, total_k=21
        )
    with pytest.raises(FederatedCapError):
        await svc.search(
            principal=None,
            query="x",
            mode="entity",
            graph_ids=[uuid.UUID(_G1.id), uuid.UUID(_G2.id), uuid.UUID(_G3.id)],
            per_graph_k=5,
            total_k=20,
        )


# ── merge + labeling ─────────────────────────────────────────────────────────────────────────


async def test_results_are_labeled_with_their_source_graph() -> None:
    repo = _FakeRepo(
        {
            _G1.id: [_row(_G1.id, "a", 0.9)],
            _G2.id: [_row(_G2.id, "b", 0.95)],
        }
    )
    out = await _svc(repo).search(
        principal=None, query="x", mode="semantic", graph_ids=None, per_graph_k=5, total_k=50
    )
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["a"]["source_graph_id"] == _G1.id
    assert by_id["a"]["source_graph_name"] == "research"
    assert by_id["b"]["source_graph_id"] == _G2.id
    # higher cosine first across graphs
    assert [r["id"] for r in out["results"]] == ["b", "a"]


def test_merge_score_desc_applies_the_total_cap_deterministically() -> None:
    # GENUINE scores (semantic): global score-desc, ties break by (graph id, node id).
    per_graph = [
        (_G1, [_row(_G1.id, "a", 0.9), _row(_G1.id, "b", 0.5)]),
        (_G2, [_row(_G2.id, "c", 0.95)]),
    ]
    merged = merge_score_desc(per_graph, total_cap=2)
    assert [r["id"] for r in merged] == ["c", "a"]  # highest cosine across graphs first


def test_interleave_round_robin_draws_fairly_across_graphs() -> None:
    # CONSTANT scores (entity/fulltext): the total cap must NOT fill from the first graph by UUID.
    # Round-robin: rank-1 of each graph, then rank-2 — so a 3-slot cap takes one from each graph.
    per_graph = [
        (_G1, [_row(_G1.id, "a1"), _row(_G1.id, "a2"), _row(_G1.id, "a3")]),
        (_G2, [_row(_G2.id, "b1"), _row(_G2.id, "b2")]),
        (_G3, [_row(_G3.id, "c1")]),
    ]
    merged = interleave_round_robin(per_graph, total_cap=3)
    assert [r["id"] for r in merged] == ["a1", "b1", "c1"]  # one per graph, not a1/a2/a3
    assert {r["source_graph_id"] for r in merged} == {_G1.id, _G2.id, _G3.id}


def test_interleave_preserves_each_graphs_internal_order() -> None:
    per_graph = [
        (_G1, [_row(_G1.id, "a1"), _row(_G1.id, "a2")]),
        (_G2, [_row(_G2.id, "b1")]),
    ]
    merged = interleave_round_robin(per_graph, total_cap=10)
    assert [r["id"] for r in merged] == ["a1", "b1", "a2"]  # rank-1s, then a's rank-2


async def test_entity_mode_does_not_starve_trailing_graphs_under_the_total_cap() -> None:
    # All-1.0 entity scores across three graphs, total cap 2 — the merge must reach >1 graph,
    # never just the first-by-UUID graph (the degenerate global-sort behaviour).
    repo = _FakeRepo(
        {
            _G1.id: [_row(_G1.id, "a1"), _row(_G1.id, "a2")],
            _G2.id: [_row(_G2.id, "b1")],
            _G3.id: [_row(_G3.id, "c1")],
        }
    )
    out = await _svc(repo).search(
        principal=None, query="x", mode="entity", graph_ids=None, per_graph_k=5, total_k=2
    )
    assert {r["source_graph_id"] for r in out["results"]} == {_G1.id, _G2.id}


def test_rrf_fuse_keys_by_graph_and_node_so_twins_do_not_collapse() -> None:
    a = {
        "id": "n1",
        "type": "Chunk",
        "properties": {},
        "source_graph_id": _G1.id,
        "source_graph_name": "research",
    }
    b = dict(a, source_graph_id=_G2.id, source_graph_name="sales")
    # Inputs are now per-graph ranked lists (one list per graph) per constituent ranking.
    fused = rrf_fuse([[[a], [b]]], total_cap=10)
    assert len(fused) == 2  # same node id in TWO graphs stays two results
    assert all("rrf_score" in r["properties"] for r in fused)


def test_rrf_uses_per_graph_local_rank_not_global_merge_position() -> None:
    # A node ranked #1 in its OWN graph must get the rank-1 RRF weight regardless of how many
    # other graphs' nodes precede it in any global ordering — fusion is graph-order independent.
    g1_top = {
        "id": "x",
        "type": "Chunk",
        "properties": {},
        "source_graph_id": _G1.id,
        "source_graph_name": "research",
    }
    g2_top = dict(g1_top, id="y", source_graph_id=_G2.id, source_graph_name="sales")
    # Both are rank-1 in their own graph's list, so both earn the same 1/(60+1) weight.
    fused = rrf_fuse([[[g1_top], [g2_top]]], total_cap=10)
    weights = {r["id"]: r["properties"]["rrf_score"] for r in fused}
    assert weights["x"] == pytest.approx(weights["y"])


# ── degrade ──────────────────────────────────────────────────────────────────────────────────


async def test_embedder_failure_degrades_semantic_to_empty_and_flags_it() -> None:
    repo = _FakeRepo({_G1.id: [_row(_G1.id, "a")]})
    out = await _svc(repo, embedder=_FailingEmbedder()).search(
        principal=None, query="x", mode="semantic", graph_ids=None, per_graph_k=5, total_k=50
    )
    assert out["results"] == []
    assert out["meta"]["semantic_degraded"] is True
    assert repo.calls == []  # no semantic branch ran without a query vector


async def test_hybrid_still_serves_fulltext_when_the_embedder_is_off() -> None:
    repo = _FakeRepo({_G1.id: [_row(_G1.id, "a")]})
    out = await _svc(repo, embedder=_FailingEmbedder()).search(
        principal=None, query="x", mode="hybrid", graph_ids=None, per_graph_k=5, total_k=50
    )
    assert [r["id"] for r in out["results"]] == ["a"]
    assert out["meta"]["semantic_degraded"] is True
    assert {m for m, _ in repo.calls} == {"fulltext"}  # only the lexical branches ran


# ── partial result (branch failure) + empty-list semantics ─────────────────────────────────────


class _OneGraphFailsRepo(_FakeRepo):
    """Entity branch raises for one graph; the others succeed (a per-graph Neo4j fault)."""

    def __init__(self, rows_by_graph, failing_graph_id: str) -> None:
        super().__init__(rows_by_graph)
        self._failing = failing_graph_id

    def entity_search(self, *, graph_id, term, top_k):
        self.calls.append(("entity", graph_id))
        if graph_id == self._failing:
            raise RuntimeError("neo4j branch error")
        return list(self._rows.get(graph_id, []))[:top_k]


async def test_one_graphs_branch_failure_yields_partial_result_not_a_500() -> None:
    repo = _OneGraphFailsRepo(
        {_G1.id: [_row(_G1.id, "a")], _G3.id: [_row(_G3.id, "c")]},
        failing_graph_id=_G2.id,
    )
    out = await _svc(repo).search(
        principal=None, query="x", mode="entity", graph_ids=None, per_graph_k=5, total_k=50
    )
    sources = {r["source_graph_id"] for r in out["results"]}
    assert sources == {_G1.id, _G3.id}  # the two healthy graphs still returned
    assert out["meta"]["graphs_failed"] == [_G2.id]  # the failed branch is reported, not raised


async def test_explicit_empty_graph_ids_is_a_caller_error() -> None:
    with pytest.raises(FederatedCapError):
        await _svc().search(
            principal=None, query="x", mode="entity", graph_ids=[], per_graph_k=5, total_k=50
        )


# ── subgraph aggregate cap ───────────────────────────────────────────────────────────────────


class _SubgraphRepo(_FakeRepo):
    """Entity match returns one anchor per graph; the neighborhood returns `n` nodes per graph."""

    def __init__(self, nodes_per_graph: int) -> None:
        super().__init__()
        self._n = nodes_per_graph

    def entity_search(self, *, graph_id, term, top_k):
        self.calls.append(("entity", graph_id))
        return [{"id": f"{graph_id}-anchor", "labels": ["X"], "props": {}, "score": 1.0}]

    def entity_neighborhood(self, *, graph_id, node_ids, limit):
        self.calls.append(("neighborhood", graph_id))
        nodes = [{"id": f"{graph_id}-n{i}", "labels": ["X"], "props": {}} for i in range(self._n)]
        return {"nodes": nodes, "edges": []}


async def test_subgraph_aggregate_cap_bounds_the_merged_node_count_across_graphs() -> None:
    # 3 graphs × 10 nodes each = 30 available; the cross-graph aggregate cap is 12, so the merged
    # result is bounded to 12 TOTAL (not 3 × per-graph), drawn fairly across graphs.
    repo = _SubgraphRepo(nodes_per_graph=10)
    svc = _Service(repo, _FakeRegistry([_G1, _G2, _G3]), max_subgraph_nodes=12)
    out = await svc.neighborhood(
        principal=None, query="x", graph_ids=None, entities_per_graph=5, limit_per_graph=10
    )
    assert len(out["nodes"]) == 12
    # fair draw: each graph contributed (cap split three ways = 4 each)
    per_graph: dict[str, int] = {}
    for n in out["nodes"]:
        per_graph[n["source_graph_id"]] = per_graph.get(n["source_graph_id"], 0) + 1
    assert per_graph == {_G1.id: 4, _G2.id: 4, _G3.id: 4}


async def test_subgraph_cap_breach_message_names_the_actual_field() -> None:
    svc = _Service(_FakeRepo(), _FakeRegistry([_G1]), max_per_graph_k=5)
    with pytest.raises(FederatedCapError, match="limit_per_graph"):
        await svc.neighborhood(
            principal=None, query="x", graph_ids=None, entities_per_graph=5, limit_per_graph=99
        )
    with pytest.raises(FederatedCapError, match="entities_per_graph"):
        await svc.neighborhood(
            principal=None, query="x", graph_ids=None, entities_per_graph=99, limit_per_graph=5
        )
