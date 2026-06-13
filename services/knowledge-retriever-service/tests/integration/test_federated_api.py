"""KRS federated HTTP layer (#330) — real routes + dev-auth + a fake federated service.
Auth (401), the labeled-result envelope, validation (422) and the fail-closed error map
(403 inaccessible subset / 422 cap / 503 registry-down) are real route behaviour.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_knowledge_retriever_service.core.dependencies import get_federated_service
from oraclous_knowledge_retriever_service.services.federated_service import (
    FederatedAccessError,
    FederatedCapError,
)
from oraclous_knowledge_retriever_service.services.graph_registry_client import GraphRegistryError

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}
_G1 = "11111111-1111-1111-1111-111111111111"
_HIT = {
    "id": "4:x:1",
    "type": "Chunk",
    "properties": {"text": "ada lovelace", "score": 0.9},
    "source_graph_id": _G1,
    "source_graph_name": "research",
}
_META = {
    "graphs_queried": [{"id": _G1, "name": "research"}],
    "graphs_skipped": [],
    "mode": "hybrid",
    "semantic_degraded": False,
}


class _FakeFederatedService:
    def __init__(self) -> None:
        self.raise_with: Exception | None = None
        self.search_calls: list[dict] = []

    async def search(self, *, principal, query, mode, graph_ids, per_graph_k, total_k):
        self.search_calls.append(
            {
                "query": query,
                "mode": mode,
                "graph_ids": graph_ids,
                "per_graph_k": per_graph_k,
                "total_k": total_k,
            }
        )
        if self.raise_with is not None:
            raise self.raise_with
        return {"results": [_HIT], "meta": dict(_META, mode=mode)}

    async def neighborhood(
        self, *, principal, query, graph_ids, entities_per_graph, limit_per_graph
    ):
        if self.raise_with is not None:
            raise self.raise_with
        edge = {
            "source": "4:x:1",
            "target": "4:y:2",
            "type": "MENTIONS",
            "properties": {},
            "source_graph_id": _G1,
            "source_graph_name": "research",
        }
        return {"nodes": [_HIT], "edges": [edge], "meta": dict(_META, mode="entity")}


@pytest.fixture
def svc() -> _FakeFederatedService:
    return _FakeFederatedService()


@pytest.fixture
def client(app, async_client, svc):
    app.dependency_overrides[get_federated_service] = lambda: svc
    yield async_client
    app.dependency_overrides.clear()


async def test_federated_search_requires_auth(client) -> None:
    resp = await client.post("/v1/federated/search", json={"query": "ada"})
    assert resp.status_code == 401


async def test_federated_search_returns_labeled_results(client, svc) -> None:
    resp = await client.post(
        "/v1/federated/search", json={"query": "ada", "mode": "entity"}, headers=_AUTH
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    hit = body["results"][0]
    # the strict federated envelope: NodeResult + the two source-graph labels (ADR-026)
    assert set(hit.keys()) == {"id", "type", "properties", "source_graph_id", "source_graph_name"}
    assert hit["source_graph_id"] == _G1 and hit["source_graph_name"] == "research"
    assert body["meta"]["mode"] == "entity"
    assert svc.search_calls[0]["mode"] == "entity"


async def test_defaults_flow_to_the_service(client, svc) -> None:
    resp = await client.post("/v1/federated/search", json={"query": "ada"}, headers=_AUTH)
    assert resp.status_code == 200
    call = svc.search_calls[0]
    assert call["mode"] == "hybrid" and call["graph_ids"] is None
    assert call["per_graph_k"] == 10 and call["total_k"] == 50


async def test_empty_query_is_422(client) -> None:
    resp = await client.post("/v1/federated/search", json={"query": ""}, headers=_AUTH)
    assert resp.status_code == 422


async def test_explicit_empty_graph_ids_is_422_at_the_schema(client) -> None:
    # An explicit empty selection is a caller error (never silently "all") — rejected by the
    # min_length=1 schema before the service runs.
    resp = await client.post(
        "/v1/federated/search", json={"query": "ada", "graph_ids": []}, headers=_AUTH
    )
    assert resp.status_code == 422


async def test_partial_result_meta_graphs_failed_passes_through(client, svc) -> None:
    # The route relays meta.graphs_failed (a partial result over the healthy graphs) unchanged.
    async def _search_with_failure(**_kw):
        return {
            "results": [_HIT],
            "meta": dict(_META, mode="entity", graphs_failed=["dead-graph-id"]),
        }

    svc.search = _search_with_failure  # type: ignore[method-assign]
    resp = await client.post(
        "/v1/federated/search", json={"query": "ada", "mode": "entity"}, headers=_AUTH
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["meta"]["graphs_failed"] == ["dead-graph-id"]


async def test_inaccessible_subset_maps_to_403(client, svc) -> None:
    svc.raise_with = FederatedAccessError("one or more requested graphs are not accessible")
    resp = await client.post(
        "/v1/federated/search",
        json={"query": "ada", "graph_ids": [str(uuid.uuid4())]},
        headers=_AUTH,
    )
    assert resp.status_code == 403


async def test_cap_breach_maps_to_422(client, svc) -> None:
    svc.raise_with = FederatedCapError("per_graph_k exceeds the configured cap")
    resp = await client.post(
        "/v1/federated/search", json={"query": "ada", "per_graph_k": 9999}, headers=_AUTH
    )
    assert resp.status_code == 422


async def test_registry_down_maps_to_503(client, svc) -> None:
    svc.raise_with = GraphRegistryError("down")
    resp = await client.post("/v1/federated/search", json={"query": "ada"}, headers=_AUTH)
    assert resp.status_code == 503


async def test_federated_subgraph_returns_labeled_nodes_and_edges(client) -> None:
    resp = await client.post("/v1/federated/subgraph", json={"query": "ada"}, headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"nodes", "edges", "meta"}
    assert body["nodes"][0]["source_graph_id"] == _G1
    edge = body["edges"][0]
    assert set(edge.keys()) == {
        "source",
        "target",
        "type",
        "properties",
        "source_graph_id",
        "source_graph_name",
    }


async def test_federated_subgraph_requires_auth(client) -> None:
    resp = await client.post("/v1/federated/subgraph", json={"query": "ada"})
    assert resp.status_code == 401
