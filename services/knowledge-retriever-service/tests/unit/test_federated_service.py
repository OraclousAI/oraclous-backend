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
    merge_ranked,
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


def test_merge_ranked_applies_the_total_cap_deterministically() -> None:
    per_graph = [
        (_G1, [_row(_G1.id, "a", 0.5), _row(_G1.id, "b", 0.5)]),
        (_G2, [_row(_G2.id, "c", 0.5)]),
    ]
    merged = merge_ranked(per_graph, total_cap=2)
    assert [r["id"] for r in merged] == ["a", "b"]  # ties break by (graph id, node id)


def test_rrf_fuse_keys_by_graph_and_node_so_twins_do_not_collapse() -> None:
    a = {
        "id": "n1",
        "type": "Chunk",
        "properties": {},
        "source_graph_id": _G1.id,
        "source_graph_name": "research",
    }
    b = dict(a, source_graph_id=_G2.id, source_graph_name="sales")
    fused = rrf_fuse([[a], [b]], total_cap=10)
    assert len(fused) == 2  # same node id in TWO graphs stays two results
    assert all("rrf_score" in r["properties"] for r in fused)


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
