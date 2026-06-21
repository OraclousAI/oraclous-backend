"""KRS search/graph HTTP layer (R3.5) — real routes + dev-auth + a fake retrieval service.
Auth (401), the NodeResult envelope, and request validation (422) are real route behaviour.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_knowledge_retriever_service.contracts import EdgeResult, NodeResult, SubgraphResult
from oraclous_knowledge_retriever_service.core.dependencies import get_retrieval_service

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}


class _FakeRetrievalService:
    async def semantic(self, *, graph_id, query, top_k):
        return [
            NodeResult(id="4:x:1", type="Chunk", properties={"text": "ada lovelace", "score": 0.9})
        ]

    async def fulltext(self, *, graph_id, query, top_k):
        return [NodeResult(id="4:x:1", type="Chunk", properties={"text": "ada", "score": 2.1})]

    async def hybrid(self, *, graph_id, query, top_k):
        return [NodeResult(id="4:x:1", type="Chunk", properties={"text": "ada", "rrf_score": 0.03})]

    async def neighbors(self, *, graph_id, node_id, top_k):
        return [NodeResult(id="4:y:2", type="Record", properties={"relationship": "PART_OF"})]

    async def similar(self, *, graph_id, node_id, top_k, min_score):
        return [
            NodeResult(
                id="4:y:2",
                type="Item",
                properties={"relationship": "SIMILAR_TO", "score": 0.91},
            )
        ]

    async def temporal(self, *, graph_id, as_of, top_k):
        return [NodeResult(id="4:z:3", type="Record", properties={"valid_from": as_of})]

    async def subgraph(self, *, graph_id, limit):
        return SubgraphResult(
            nodes=[
                NodeResult(id="4:x:1", type="Document", properties={"name": "A"}),
                NodeResult(id="4:y:2", type="Chunk", properties={"text": "b"}),
            ],
            edges=[
                EdgeResult(source="4:x:1", target="4:y:2", type="HAS_CHUNK", properties={}),
                EdgeResult(
                    source="4:x:1",
                    target="4:y:2",
                    type="SIMILAR_TO",
                    properties={"score": 0.87},
                ),
            ],
        )


@pytest.fixture
def client(app, async_client):
    app.dependency_overrides[get_retrieval_service] = lambda: _FakeRetrievalService()
    yield async_client
    app.dependency_overrides.clear()


async def test_semantic_requires_auth(client) -> None:
    resp = await client.post(
        "/v1/search/semantic", json={"query": "x", "graph_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 401


async def test_semantic_returns_node_result_envelope(client) -> None:
    resp = await client.post(
        "/v1/search/semantic",
        json={"query": "who wrote it", "graph_id": str(uuid.uuid4()), "top_k": 5},
        headers=_AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert set(body[0].keys()) == {"id", "type", "properties"}  # strict envelope
    assert body[0]["type"] == "Chunk"
    assert body[0]["properties"]["text"] == "ada lovelace"


async def test_empty_query_is_422(client) -> None:
    resp = await client.post(
        "/v1/search/semantic", json={"query": "", "graph_id": str(uuid.uuid4())}, headers=_AUTH
    )
    assert resp.status_code == 422


async def test_fulltext_and_hybrid_and_graph(client) -> None:
    gid = str(uuid.uuid4())
    ft = await client.post(
        "/v1/search/fulltext", json={"query": "ada", "graph_id": gid}, headers=_AUTH
    )
    assert ft.status_code == 200 and ft.json()[0]["type"] == "Chunk"
    hy = await client.post(
        "/v1/search/hybrid", json={"query": "ada", "graph_id": gid}, headers=_AUTH
    )
    assert hy.status_code == 200 and "rrf_score" in hy.json()[0]["properties"]
    nb = await client.get(f"/v1/graph/{gid}/neighbors/4:x:1", headers=_AUTH)
    assert nb.status_code == 200 and nb.json()[0]["properties"]["relationship"] == "PART_OF"
    tp = await client.get(f"/v1/graph/{gid}/temporal?as_of=2020-01-01", headers=_AUTH)
    assert tp.status_code == 200 and tp.json()[0]["type"] == "Record"


async def test_subgraph_returns_nodes_and_edges(client) -> None:
    gid = str(uuid.uuid4())
    resp = await client.get(f"/v1/graph/{gid}/subgraph?limit=50", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"nodes", "edges"}  # strict {nodes, edges} envelope
    assert {n["id"] for n in body["nodes"]} == {"4:x:1", "4:y:2"}
    # Edges carry a `properties` bag (mirrors nodes); an edge `score` (e.g. on SIMILAR_TO)
    # surfaces through it for the FE explorer.
    assert all(set(e.keys()) == {"source", "target", "type", "properties"} for e in body["edges"])
    assert body["edges"][0] == {
        "source": "4:x:1",
        "target": "4:y:2",
        "type": "HAS_CHUNK",
        "properties": {},
    }
    scored = next(e for e in body["edges"] if e["type"] == "SIMILAR_TO")
    assert scored["properties"]["score"] == 0.87


async def test_similar_returns_node_results(client) -> None:
    # find_similar (#310): the SIMILAR_TO neighbours surface through the NodeResult envelope with
    # the edge cosine + relationship in `properties`. top_k/min_score are accepted query params.
    gid = str(uuid.uuid4())
    resp = await client.get(f"/v1/graph/{gid}/similar/4:x:1?top_k=5&min_score=0.5", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert set(body[0].keys()) == {"id", "type", "properties"}  # strict envelope
    assert body[0]["properties"]["relationship"] == "SIMILAR_TO"
    assert body[0]["properties"]["score"] == 0.91


async def test_similar_requires_auth(client) -> None:
    resp = await client.get(f"/v1/graph/{uuid.uuid4()}/similar/4:x:1")
    assert resp.status_code == 401


async def test_subgraph_requires_auth(client) -> None:
    resp = await client.get(f"/v1/graph/{uuid.uuid4()}/subgraph")
    assert resp.status_code == 401
