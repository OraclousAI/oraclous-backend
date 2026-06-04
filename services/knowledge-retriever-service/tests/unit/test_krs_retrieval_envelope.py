"""NodeResult envelope tests for all five KRS retrieval endpoints (ORAA-60).

[R3-KRS-3] Acceptance criteria:
1. POST /v1/search/semantic, /v1/search/fulltext, /v1/search/hybrid,
   GET /v1/graph/traverse, GET /v1/graph/temporal each return HTTP 200
   with a list of NodeResult-shaped items.
2. Every item in the response list has at minimum the OHM envelope fields:
   ``id`` (str), ``type`` (str), ``properties`` (dict).
3. Modality-specific data lives inside ``properties`` — not at the top level
   of the response item.

All imports are function-local (TST001 / ORA-48) so this file collects
cleanly even if the routers are not yet wired.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_SEARCH_QUERY = {"query": "knowledge graph test query"}
_NODE_ID = "node-123"
_TIMESTAMP = "2026-06-04T00:00:00Z"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _assert_node_result_shape(item: object) -> None:
    """Assert that *item* satisfies the NodeResult envelope contract."""
    assert isinstance(item, dict), f"NodeResult item must be a dict; got {type(item)}"
    assert "id" in item, "NodeResult item missing required field 'id'"
    assert "type" in item, "NodeResult item missing required field 'type'"
    assert "properties" in item, "NodeResult item missing required field 'properties'"
    assert isinstance(item["id"], str), f"NodeResult.id must be str; got {type(item['id'])}"
    assert isinstance(item["type"], str), f"NodeResult.type must be str; got {type(item['type'])}"
    assert isinstance(item["properties"], dict), (
        f"NodeResult.properties must be dict; got {type(item['properties'])}"
    )


async def _make_client():
    from httpx import ASGITransport, AsyncClient  # ORA-48
    from oraclous_knowledge_retriever_service.app.factory import create_app  # ORA-48

    app = create_app()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# POST /v1/search/semantic
# ---------------------------------------------------------------------------


class TestSemanticSearchEnvelope:
    """POST /v1/search/semantic returns a list of NodeResult-shaped items."""

    async def test_semantic_returns_200(self) -> None:
        """POST /v1/search/semantic must return HTTP 200."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/semantic", json=_SEARCH_QUERY)
        assert response.status_code == 200

    async def test_semantic_returns_list(self) -> None:
        """Response body must be a JSON array (list)."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/semantic", json=_SEARCH_QUERY)
        assert isinstance(response.json(), list)

    async def test_semantic_items_have_node_result_shape(self) -> None:
        """Each item in the semantic search response satisfies NodeResult shape."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/semantic", json=_SEARCH_QUERY)
        items = response.json()
        assert len(items) > 0, "Semantic search must return at least one stub result"
        for item in items:
            _assert_node_result_shape(item)

    async def test_semantic_modality_fields_inside_properties(self) -> None:
        """Modality-specific data must be inside NodeResult.properties."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/semantic", json=_SEARCH_QUERY)
        item = response.json()[0]
        top_level_keys = set(item.keys())
        assert top_level_keys == {"id", "type", "properties"}, (
            f"Semantic NodeResult must have only envelope keys at root; "
            f"found extra keys: {top_level_keys - {'id', 'type', 'properties'}}"
        )


# ---------------------------------------------------------------------------
# POST /v1/search/fulltext
# ---------------------------------------------------------------------------


class TestFulltextSearchEnvelope:
    """POST /v1/search/fulltext returns a list of NodeResult-shaped items."""

    async def test_fulltext_returns_200(self) -> None:
        """POST /v1/search/fulltext must return HTTP 200."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/fulltext", json=_SEARCH_QUERY)
        assert response.status_code == 200

    async def test_fulltext_returns_list(self) -> None:
        """Response body must be a JSON array."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/fulltext", json=_SEARCH_QUERY)
        assert isinstance(response.json(), list)

    async def test_fulltext_items_have_node_result_shape(self) -> None:
        """Each item in the fulltext search response satisfies NodeResult shape."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/fulltext", json=_SEARCH_QUERY)
        items = response.json()
        assert len(items) > 0, "Fulltext search must return at least one stub result"
        for item in items:
            _assert_node_result_shape(item)

    async def test_fulltext_modality_fields_inside_properties(self) -> None:
        """Modality-specific data must be inside NodeResult.properties."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/fulltext", json=_SEARCH_QUERY)
        item = response.json()[0]
        assert set(item.keys()) == {"id", "type", "properties"}


# ---------------------------------------------------------------------------
# POST /v1/search/hybrid
# ---------------------------------------------------------------------------


class TestHybridSearchEnvelope:
    """POST /v1/search/hybrid returns a list of NodeResult-shaped items."""

    async def test_hybrid_returns_200(self) -> None:
        """POST /v1/search/hybrid must return HTTP 200."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/hybrid", json=_SEARCH_QUERY)
        assert response.status_code == 200

    async def test_hybrid_returns_list(self) -> None:
        """Response body must be a JSON array."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/hybrid", json=_SEARCH_QUERY)
        assert isinstance(response.json(), list)

    async def test_hybrid_items_have_node_result_shape(self) -> None:
        """Each item in the hybrid search response satisfies NodeResult shape."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/hybrid", json=_SEARCH_QUERY)
        items = response.json()
        assert len(items) > 0, "Hybrid search must return at least one stub result"
        for item in items:
            _assert_node_result_shape(item)

    async def test_hybrid_modality_fields_inside_properties(self) -> None:
        """Modality-specific data must be inside NodeResult.properties."""
        async with await _make_client() as client:
            response = await client.post("/v1/search/hybrid", json=_SEARCH_QUERY)
        item = response.json()[0]
        assert set(item.keys()) == {"id", "type", "properties"}


# ---------------------------------------------------------------------------
# GET /v1/graph/traverse
# ---------------------------------------------------------------------------


class TestGraphTraverseEnvelope:
    """GET /v1/graph/traverse returns a list of NodeResult-shaped items."""

    async def test_traverse_returns_200(self) -> None:
        """GET /v1/graph/traverse must return HTTP 200."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/traverse", params={"node_id": _NODE_ID})
        assert response.status_code == 200

    async def test_traverse_returns_list(self) -> None:
        """Response body must be a JSON array."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/traverse", params={"node_id": _NODE_ID})
        assert isinstance(response.json(), list)

    async def test_traverse_items_have_node_result_shape(self) -> None:
        """Each item in the traverse response satisfies NodeResult shape."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/traverse", params={"node_id": _NODE_ID})
        items = response.json()
        assert len(items) > 0, "Traverse must return at least one stub result"
        for item in items:
            _assert_node_result_shape(item)

    async def test_traverse_modality_fields_inside_properties(self) -> None:
        """Modality-specific data (traversal depth, edges) must be in properties."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/traverse", params={"node_id": _NODE_ID})
        item = response.json()[0]
        assert set(item.keys()) == {"id", "type", "properties"}

    async def test_traverse_missing_node_id_returns_422(self) -> None:
        """GET /v1/graph/traverse without node_id must return 422."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/traverse")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/graph/temporal
# ---------------------------------------------------------------------------


class TestTemporalSliceEnvelope:
    """GET /v1/graph/temporal returns a list of NodeResult-shaped items."""

    async def test_temporal_returns_200(self) -> None:
        """GET /v1/graph/temporal must return HTTP 200."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/temporal", params={"ts": _TIMESTAMP})
        assert response.status_code == 200

    async def test_temporal_returns_list(self) -> None:
        """Response body must be a JSON array."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/temporal", params={"ts": _TIMESTAMP})
        assert isinstance(response.json(), list)

    async def test_temporal_items_have_node_result_shape(self) -> None:
        """Each item in the temporal response satisfies NodeResult shape."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/temporal", params={"ts": _TIMESTAMP})
        items = response.json()
        assert len(items) > 0, "Temporal slice must return at least one stub result"
        for item in items:
            _assert_node_result_shape(item)

    async def test_temporal_modality_fields_inside_properties(self) -> None:
        """Modality-specific data (timestamp, bounds) must be in properties."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/temporal", params={"ts": _TIMESTAMP})
        item = response.json()[0]
        assert set(item.keys()) == {"id", "type", "properties"}

    async def test_temporal_missing_ts_returns_422(self) -> None:
        """GET /v1/graph/temporal without ts must return 422."""
        async with await _make_client() as client:
            response = await client.get("/v1/graph/temporal")
        assert response.status_code == 422
