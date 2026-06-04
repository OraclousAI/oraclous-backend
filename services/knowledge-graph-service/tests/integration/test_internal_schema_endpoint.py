"""Contract tests — GET /internal/v1/schema/{graph_id} (ORAA-57).

Defines the expected behaviour of the internal schema-lookup endpoint that
knowledge-retriever-service uses to obtain the OHM-schema envelope for a graph.

Endpoint contract:
  GET /internal/v1/schema/{graph_id}
    200  — known graph → OHM-schema envelope (GraphSchema serialised as JSON)
    404  — unknown graph (schema_manager raises LookupError)
  No bearer-token auth — internal (service-to-service) traffic only.

Patch target:
  oraclous_knowledge_graph_service.api.schema.schema_manager
  (same singleton used by the public /api/v1/schema/info endpoint)

All imports of the SUT are function-local (ORA-48 / TST001) — pytest
--collect-only succeeds while the internal router is not yet wired.

RED until the internal router is mounted in create_app().
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.contract]

_NS = "oraclous_knowledge_graph_service"
_SCHEMA_MGR = f"{_NS}.api.schema.schema_manager"

GRAPH_ID = str(uuid.uuid4())
UNKNOWN_GRAPH_ID = str(uuid.uuid4())

_LAST_UPDATED = datetime(2026, 6, 4, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _graph_schema(graph_id: str = GRAPH_ID):
    """Build a GraphSchema for the given graph_id."""
    from oraclous_knowledge_graph_service.api.schema import GraphSchema, NodeSchema  # ORA-48

    return GraphSchema(
        graph_id=graph_id,
        nodes={
            "Company": NodeSchema(
                label="Company",
                properties={"name": "string", "graph_id": "string"},
                sample_count=5,
                indexes=["Company.name"],
            ),
            "Person": NodeSchema(
                label="Person",
                properties={"name": "string", "email": "string"},
                sample_count=12,
                indexes=[],
            ),
        },
        relationships={"WORKS_FOR": {"from": "Person", "to": "Company"}},
        constraints=["UNIQUE Company.name"],
        indexes=["Company.name"],
        last_updated=_LAST_UPDATED,
        schema_version="v1",
    )


# ---------------------------------------------------------------------------
# 1. Happy path — 200 with OHM-schema envelope
# ---------------------------------------------------------------------------


class TestInternalSchemaHappyPath:
    """Known graph_id → 200 with complete OHM-schema envelope."""

    async def test_known_graph_returns_200(self, async_client) -> None:
        """GET /internal/v1/schema/{graph_id} returns HTTP 200 for a known graph."""
        schema = _graph_schema()
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema)
            response = await async_client.get(f"/internal/v1/schema/{GRAPH_ID}")

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}\nBody: {response.text}"
        )

    async def test_response_is_json(self, async_client) -> None:
        """Response must carry Content-Type: application/json."""
        schema = _graph_schema()
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema)
            response = await async_client.get(f"/internal/v1/schema/{GRAPH_ID}")

        assert "application/json" in response.headers.get("content-type", "")

    async def test_envelope_contains_graph_id(self, async_client) -> None:
        """Response envelope graph_id must match the path parameter."""
        schema = _graph_schema()
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema)
            response = await async_client.get(f"/internal/v1/schema/{GRAPH_ID}")

        body = response.json()
        assert body["graph_id"] == GRAPH_ID, (
            f"envelope graph_id {body.get('graph_id')!r} != path param {GRAPH_ID!r}"
        )

    async def test_envelope_contains_schema_version(self, async_client) -> None:
        """Response envelope must include a non-empty schema_version field."""
        schema = _graph_schema()
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema)
            response = await async_client.get(f"/internal/v1/schema/{GRAPH_ID}")

        body = response.json()
        assert "schema_version" in body
        assert isinstance(body["schema_version"], str) and body["schema_version"].strip()

    async def test_envelope_contains_nodes_dict(self, async_client) -> None:
        """Response envelope must include a 'nodes' mapping of label → NodeSchema."""
        schema = _graph_schema()
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema)
            response = await async_client.get(f"/internal/v1/schema/{GRAPH_ID}")

        body = response.json()
        assert "nodes" in body, "envelope must have a 'nodes' key"
        assert isinstance(body["nodes"], dict)
        assert "Company" in body["nodes"], "nodes must include the 'Company' label"
        assert "Person" in body["nodes"], "nodes must include the 'Person' label"

    async def test_node_schema_shape(self, async_client) -> None:
        """Each entry in 'nodes' must include label, properties, sample_count, indexes."""
        schema = _graph_schema()
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema)
            response = await async_client.get(f"/internal/v1/schema/{GRAPH_ID}")

        node = response.json()["nodes"]["Company"]
        for field in ("label", "properties", "sample_count", "indexes"):
            assert field in node, f"NodeSchema missing required field '{field}'"
        assert isinstance(node["properties"], dict)
        assert isinstance(node["indexes"], list)

    async def test_envelope_contains_last_updated_iso8601(self, async_client) -> None:
        """Response envelope must include a parseable ISO-8601 last_updated timestamp."""
        schema = _graph_schema()
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema)
            response = await async_client.get(f"/internal/v1/schema/{GRAPH_ID}")

        body = response.json()
        assert "last_updated" in body
        # Must be parseable as an ISO-8601 datetime
        dt = datetime.fromisoformat(body["last_updated"])
        assert dt is not None

    async def test_schema_manager_called_with_correct_graph_id(self, async_client) -> None:
        """schema_manager.extract_schema must be called with the path-param graph_id."""
        schema = _graph_schema()
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema)
            await async_client.get(f"/internal/v1/schema/{GRAPH_ID}")

        called_with = mock_mgr.extract_schema.call_args
        called_id = called_with.args[0] if called_with.args else called_with.kwargs.get("graph_id")
        assert called_id == GRAPH_ID, (
            f"extract_schema called with {called_id!r}, expected {GRAPH_ID!r}"
        )


# ---------------------------------------------------------------------------
# 2. Not-found — 404 for unknown graph
# ---------------------------------------------------------------------------


class TestInternalSchemaNotFound:
    """Unknown graph_id → 404; response body must not leak internal details."""

    async def test_unknown_graph_returns_404(self, async_client) -> None:
        """GET /internal/v1/schema/{unknown_id} → HTTP 404."""
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(side_effect=LookupError("graph not found"))
            response = await async_client.get(f"/internal/v1/schema/{UNKNOWN_GRAPH_ID}")

        assert response.status_code == 404, (
            f"Expected 404 for unknown graph, got {response.status_code}\nBody: {response.text}"
        )

    async def test_404_body_has_detail_field(self, async_client) -> None:
        """404 response must carry a 'detail' key — FastAPI error envelope convention."""
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(side_effect=LookupError("graph not found"))
            response = await async_client.get(f"/internal/v1/schema/{UNKNOWN_GRAPH_ID}")

        assert response.status_code == 404
        body = response.json()
        assert "detail" in body, "404 response must include 'detail' field"

    async def test_404_body_does_not_expose_internal_exception_message(self, async_client) -> None:
        """404 response must not echo the raw LookupError message or stack trace."""
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(
                side_effect=LookupError("INTERNAL: neo4j returned empty for g-abc123")
            )
            response = await async_client.get(f"/internal/v1/schema/{UNKNOWN_GRAPH_ID}")

        assert response.status_code == 404
        text = response.text
        assert "neo4j" not in text.lower(), "404 must not leak storage implementation details"
        assert "Traceback" not in text
        assert "LookupError" not in text

    async def test_different_graph_ids_each_get_independent_lookup(self, async_client) -> None:
        """Each call to the endpoint triggers a fresh schema_manager.extract_schema call."""
        schema_a = _graph_schema(graph_id=GRAPH_ID)
        id_b = str(uuid.uuid4())

        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema_a)
            await async_client.get(f"/internal/v1/schema/{GRAPH_ID}")
            await async_client.get(f"/internal/v1/schema/{id_b}")

        assert mock_mgr.extract_schema.call_count == 2
        calls = [
            c.args[0] if c.args else c.kwargs.get("graph_id")
            for c in mock_mgr.extract_schema.call_args_list
        ]
        assert GRAPH_ID in calls
        assert id_b in calls


# ---------------------------------------------------------------------------
# 3. Auth-free — internal endpoint must not require a bearer token
# ---------------------------------------------------------------------------


class TestInternalSchemaNoAuthRequired:
    """The /internal/v1/schema endpoint is service-to-service; no bearer token needed."""

    async def test_request_without_auth_header_is_not_rejected(self, async_client) -> None:
        """GET /internal/v1/schema/{graph_id} without Authorization → not 401/403."""
        schema = _graph_schema()
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema)
            response = await async_client.get(
                f"/internal/v1/schema/{GRAPH_ID}"
                # No Authorization header
            )

        assert response.status_code not in (401, 403), (
            "Internal schema endpoint must not demand a bearer token"
        )

    async def test_internal_path_not_exposed_under_api_v1(self, async_client) -> None:
        """The internal endpoint must NOT be reachable at /api/v1/schema/... prefix."""
        schema = _graph_schema()
        with patch(_SCHEMA_MGR) as mock_mgr:
            mock_mgr.extract_schema = AsyncMock(return_value=schema)
            response = await async_client.get(f"/api/v1/schema/{GRAPH_ID}")

        # Should be 404 (route not found) — not 200 — because the internal
        # endpoint lives under /internal/v1/, not /api/v1/.
        assert response.status_code == 404, (
            "Internal schema endpoint must not be exposed under the /api/v1/ prefix"
        )
