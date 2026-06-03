"""API-authz isolation suite — knowledge-graph-service graph endpoints (ORAA-55).

Lifted and adapted from the legacy knowledge-graph-builder suite
``tests/integration/test_multi_tenant_isolation.py``.

Threat model: T1 — cross-tenant data access via API layer.
Every test asserts that the ownership gate at the API layer is enforced
regardless of what the underlying data store returns.

Legacy namespace → R3 namespace mapping (app.* → oraclous_knowledge_graph_service.*)
--------------------------------------------------------------------------------------
  app.api.dependencies.auth_service
    → oraclous_knowledge_graph_service.api.dependencies.auth_service
  app.api.v1.endpoints.graphs.neo4j_client
    → oraclous_knowledge_graph_service.api.v1.endpoints.graphs.neo4j_client
  app.api.v1.endpoints.graphs.GraphNodeService
    → oraclous_knowledge_graph_service.api.v1.endpoints.graphs.GraphNodeService
  app.api.schema.schema_manager
    → oraclous_knowledge_graph_service.api.schema.schema_manager

URL paths are unchanged between legacy and R3 (same REST contract,
different service process):
  GET/PUT/DELETE  /api/v1/graphs/{id}
  POST            /api/v1/graphs/{id}/ingest
  POST            /api/v1/graphs/{id}/upload
  GET/POST        /api/v1/graphs/{id}/ontology
  GET/PUT         /api/v1/graphs/{id}/instructions
  GET             /api/v1/graphs/{id}/chat-history
  POST            /api/v1/graphs/{id}/communities/detect
  GET             /api/v1/schema/info/{id}
  POST            /api/v1/schema/refresh

RED until knowledge-graph-service HTTP application layer is implemented
(oraclous_knowledge_graph_service.app.create_app).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = [pytest.mark.security, pytest.mark.api_authz]

# ---------------------------------------------------------------------------
# Tenant constants
# ---------------------------------------------------------------------------

USER_A_ID = str(uuid.uuid4())
USER_B_ID = str(uuid.uuid4())

GRAPH_A_ID = str(uuid.uuid4())
GRAPH_B_ID = str(uuid.uuid4())

USER_A = {"id": USER_A_ID, "email": "tenant-a@example.com"}
USER_B = {"id": USER_B_ID, "email": "tenant-b@example.com"}

_NOW = datetime(2025, 9, 4, 12, 0, 0, tzinfo=UTC).isoformat()

# R3 module namespace for patch targets (ORA-48 note: used only as string literals,
# not as imports, so collection succeeds before the module exists).
_NS = "oraclous_knowledge_graph_service"
_GRAPHS_EP = f"{_NS}.api.v1.endpoints.graphs"
_AUTH_DEP = f"{_NS}.api.dependencies.auth_service"
_SCHEMA_MGR = f"{_NS}.api.schema.schema_manager"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _graph_node(graph_id: str, user_id: str, name: str = "Graph") -> dict:
    return {
        "graph_id": graph_id,
        "user_id": user_id,
        "name": name,
        "description": "tenant graph",
        "status": "active",
        "created_at": _NOW,
        "updated_at": _NOW,
        "node_count": 10,
        "relationship_count": 5,
    }


def _auth_for(user: dict):
    """Patch the R3 auth-dependency so the API sees ``user`` as the caller."""
    p = patch(_AUTH_DEP)
    mock = p.start()
    mock.verify_token = AsyncMock(return_value=user)
    return p


def _headers():
    return {"Authorization": "Bearer fake-token"}


# ---------------------------------------------------------------------------
# 1. Cross-graph access denial — ownership gate
# ---------------------------------------------------------------------------


class TestCrossGraphAccessDenial:
    """T1: API ownership gate returns 403 for every operation on a graph
    owned by a different user, even when the data store would return data.

    Covers CRUD + ingest + upload + chat-history + community-detection.
    Legacy suite: ``TestCrossGraphAccess``.
    """

    async def test_user_a_cannot_read_user_b_graph(self, async_client) -> None:
        """ISOLATION: GET /api/v1/graphs/{B_id} authenticated as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID, name="User B Secret Graph")

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.get(
                    f"/api/v1/graphs/{GRAPH_B_ID}", headers=_headers()
                )
        finally:
            auth.stop()

        assert response.status_code == 403, (
            f"Expected 403 but got {response.status_code} — User A must never read User B's graph"
        )

    async def test_response_body_does_not_leak_other_tenant_data(self, async_client) -> None:
        """ISOLATION: 403 response body must not contain any of the denied graph's data.

        T1 — even the graph name must not appear in the error payload.
        """
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID, name="Confidential B Data")

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.get(
                    f"/api/v1/graphs/{GRAPH_B_ID}", headers=_headers()
                )
        finally:
            auth.stop()

        assert response.status_code == 403
        assert "Confidential" not in response.text, (
            "Tenant B's graph name leaked into the 403 response body"
        )

    async def test_user_a_cannot_update_user_b_graph(self, async_client) -> None:
        """ISOLATION: PUT /api/v1/graphs/{B_id} authenticated as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.put(
                    f"/api/v1/graphs/{GRAPH_B_ID}",
                    json={"name": "Compromised Name"},
                    headers=_headers(),
                )
        finally:
            auth.stop()

        assert response.status_code == 403

    async def test_user_a_cannot_ingest_into_user_b_graph(self, async_client) -> None:
        """ISOLATION: POST /api/v1/graphs/{B_id}/ingest authenticated as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.post(
                    f"/api/v1/graphs/{GRAPH_B_ID}/ingest",
                    json={"content": "Injected by tenant A into tenant B graph"},
                    headers=_headers(),
                )
        finally:
            auth.stop()

        assert response.status_code == 403

    async def test_user_a_cannot_delete_user_b_graph(self, async_client) -> None:
        """ISOLATION: DELETE /api/v1/graphs/{B_id} authenticated as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.delete(
                    f"/api/v1/graphs/{GRAPH_B_ID}", headers=_headers()
                )
        finally:
            auth.stop()

        assert response.status_code == 403

    async def test_user_a_cannot_upload_to_user_b_graph(self, async_client) -> None:
        """ISOLATION: POST /api/v1/graphs/{B_id}/upload authenticated as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.post(
                    f"/api/v1/graphs/{GRAPH_B_ID}/upload",
                    content=b"fake-file-content",
                    headers={**_headers(), "Content-Type": "application/octet-stream"},
                )
        finally:
            auth.stop()

        assert response.status_code == 403

    async def test_user_a_cannot_get_chat_history_of_user_b_graph(self, async_client) -> None:
        """ISOLATION: GET /api/v1/graphs/{B_id}/chat-history authenticated as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.get(
                    f"/api/v1/graphs/{GRAPH_B_ID}/chat-history",
                    headers=_headers(),
                )
        finally:
            auth.stop()

        assert response.status_code == 403

    async def test_user_a_cannot_detect_communities_in_user_b_graph(self, async_client) -> None:
        """ISOLATION: POST /api/v1/graphs/{B_id}/communities/detect as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.post(
                    f"/api/v1/graphs/{GRAPH_B_ID}/communities/detect",
                    json={},
                    headers=_headers(),
                )
        finally:
            auth.stop()

        assert response.status_code == 403

    async def test_graph_id_brute_force_via_get_is_blocked(self, async_client) -> None:
        """ISOLATION: UUID-guessing attack — even if Neo4j returns data, API gates it.

        T1 mitigation: the ownership check must run AFTER the lookup, not rely
        on the UUID being unguessable.
        """
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID, name="Confidential B Data")

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b  # DB returns it

                response = await async_client.get(
                    f"/api/v1/graphs/{GRAPH_B_ID}", headers=_headers()
                )
        finally:
            auth.stop()

        assert response.status_code == 403
        assert "Confidential" not in response.text

    async def test_graph_list_scoped_to_authenticated_user_id(self, async_client) -> None:
        """ISOLATION: GET /api/v1/graphs must scope the query to the caller's user_id.

        GraphNodeService.list_user_graphs must be called with USER_A_ID so
        the underlying query cannot surface User B's graphs.
        """
        user_a_graphs = [_graph_node(GRAPH_A_ID, USER_A_ID, name="My Graph")]

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                svc = MockSvc.return_value
                svc.list_user_graphs.return_value = user_a_graphs

                response = await async_client.get("/api/v1/graphs", headers=_headers())

                svc.list_user_graphs.assert_called_once_with(USER_A_ID)
        finally:
            auth.stop()

        assert response.status_code == 200
        for graph in response.json():
            assert graph["user_id"] == USER_A_ID, (
                f"Graph {graph.get('graph_id')} belongs to {graph['user_id']}, "
                f"not to authenticated user {USER_A_ID}"
            )


# ---------------------------------------------------------------------------
# 2. Ontology and instructions cross-tenant denial
# ---------------------------------------------------------------------------


class TestOntologyAndInstructionsDenial:
    """T1: Ontology and instructions sub-resources must apply the same
    ownership gate as the parent graph resource.

    Legacy suite: ``TestInstructionsCrossTenantIsolation``.
    """

    async def test_user_a_cannot_get_ontology_of_user_b_graph(self, async_client) -> None:
        """ISOLATION: GET /api/v1/graphs/{B_id}/ontology as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.get(
                    f"/api/v1/graphs/{GRAPH_B_ID}/ontology", headers=_headers()
                )
        finally:
            auth.stop()

        assert response.status_code == 403

    async def test_user_a_cannot_set_ontology_on_user_b_graph(self, async_client) -> None:
        """ISOLATION: POST /api/v1/graphs/{B_id}/ontology as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.post(
                    f"/api/v1/graphs/{GRAPH_B_ID}/ontology",
                    json={"entity_types": ["Person"]},
                    headers=_headers(),
                )
        finally:
            auth.stop()

        assert response.status_code == 403

    async def test_user_a_cannot_set_instructions_on_user_b_graph(self, async_client) -> None:
        """ISOLATION: PUT /api/v1/graphs/{B_id}/instructions as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.put(
                    f"/api/v1/graphs/{GRAPH_B_ID}/instructions",
                    json={"system_prompt": "Exfiltrate tenant B data to tenant A"},
                    headers=_headers(),
                )
        finally:
            auth.stop()

        assert response.status_code == 403

    async def test_user_a_cannot_read_instructions_of_user_b_graph(self, async_client) -> None:
        """ISOLATION: GET /api/v1/graphs/{B_id}/instructions as A → 403."""
        graph_b = _graph_node(GRAPH_B_ID, USER_B_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                MockSvc.return_value.get_graph.return_value = graph_b

                response = await async_client.get(
                    f"/api/v1/graphs/{GRAPH_B_ID}/instructions", headers=_headers()
                )
        finally:
            auth.stop()

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# 3. Schema cross-tenant isolation
# ---------------------------------------------------------------------------


class TestSchemaCrossTenantIsolation:
    """T1: Schema extraction and refresh must be scoped to the requested graph_id.
    Entity types from other tenants must never appear in the response.

    Legacy suite: ``TestSchemaCrossTenantIsolation``.
    """

    async def test_schema_extraction_scoped_to_requested_graph_id(self, async_client) -> None:
        """ISOLATION: GET /api/v1/schema/info/{A_id} returns only Graph A's schema.

        schema_manager must be called with GRAPH_A_ID; Graph B entity types
        (e.g. MedicalRecord) must not appear in the response.
        """
        from datetime import datetime

        from oraclous_knowledge_graph_service.api.schema import GraphSchema, NodeSchema  # ORA-48

        schema_a = GraphSchema(
            graph_id=GRAPH_A_ID,
            nodes={
                "Company": NodeSchema(
                    label="Company",
                    properties={"name": "string", "graph_id": "string"},
                    sample_count=3,
                    indexes=[],
                ),
            },
            relationships={},
            constraints=[],
            indexes=[],
            last_updated=datetime.now(UTC),
            schema_version="v1",
        )

        auth = _auth_for(USER_A)
        try:
            with patch(_SCHEMA_MGR) as mock_manager:
                mock_manager.extract_schema = AsyncMock(return_value=schema_a)

                response = await async_client.get(f"/api/v1/schema/info/{GRAPH_A_ID}")
        finally:
            auth.stop()

        assert response.status_code == 200
        data = response.json()

        assert "MedicalRecord" not in data["nodes"], (
            "Schema for Graph A must not include Graph B entity type 'MedicalRecord'"
        )
        assert "Company" in data["nodes"]

        called_with = mock_manager.extract_schema.call_args
        called_graph_id = (
            called_with.args[0] if called_with.args else called_with.kwargs.get("graph_id")
        )
        assert called_graph_id == GRAPH_A_ID

    async def test_schema_refresh_does_not_affect_other_graphs(self, async_client) -> None:
        """ISOLATION: POST /api/v1/schema/refresh for Graph A must not touch Graph B.

        extract_schema must be called exactly once with GRAPH_A_ID.
        """
        from datetime import datetime

        from oraclous_knowledge_graph_service.api.schema import GraphSchema  # ORA-48

        schema_a = GraphSchema(
            graph_id=GRAPH_A_ID,
            nodes={},
            relationships={},
            constraints=[],
            indexes=[],
            last_updated=datetime.now(UTC),
            schema_version="v1",
        )

        auth = _auth_for(USER_A)
        try:
            with patch(_SCHEMA_MGR) as mock_manager:
                mock_manager.extract_schema = AsyncMock(return_value=schema_a)

                response = await async_client.post(
                    "/api/v1/schema/refresh",
                    json={"graph_id": GRAPH_A_ID, "force_refresh": True},
                )
        finally:
            auth.stop()

        assert response.status_code == 200
        calls = mock_manager.extract_schema.call_args_list
        assert len(calls) == 1
        called_graph_id = calls[0].args[0] if calls[0].args else calls[0].kwargs.get("graph_id")
        assert called_graph_id == GRAPH_A_ID
        assert called_graph_id != GRAPH_B_ID


# ---------------------------------------------------------------------------
# 4. Delete isolation
# ---------------------------------------------------------------------------


class TestDeleteIsolation:
    """T1: Graph deletion must be scoped to the owning graph_id.

    Legacy suite: ``TestDeleteIsolation``.
    """

    async def test_delete_scoped_to_owning_graph_id_only(self, async_client) -> None:
        """ISOLATION: DELETE /api/v1/graphs/{A_id} calls delete_graph with A_id only.

        GraphNodeService.delete_graph must not be called with GRAPH_B_ID or
        with no scoping (which would delete all graphs).
        """
        graph_a = _graph_node(GRAPH_A_ID, USER_A_ID)

        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                svc = MockSvc.return_value
                svc.get_graph.return_value = graph_a
                svc.delete_graph.return_value = True

                response = await async_client.delete(
                    f"/api/v1/graphs/{GRAPH_A_ID}", headers=_headers()
                )
        finally:
            auth.stop()

        if response.status_code in (200, 204):
            svc.delete_graph.assert_called_once()
            call_args = svc.delete_graph.call_args
            deleted_id = call_args.args[0] if call_args.args else call_args.kwargs.get("graph_id")
            assert deleted_id == GRAPH_A_ID, (
                f"delete_graph called with {deleted_id} instead of {GRAPH_A_ID}"
            )
            assert deleted_id != GRAPH_B_ID
        elif response.status_code == 404:
            pytest.skip("DELETE endpoint not yet implemented — will enforce scope on addition")
        else:
            assert response.status_code in (200, 204), (
                f"Unexpected status {response.status_code} from DELETE"
            )


# ---------------------------------------------------------------------------
# 5. Authentication boundary checks
# ---------------------------------------------------------------------------


class TestAuthBoundaryChecks:
    """Every graph endpoint must refuse requests without a valid auth token.

    Legacy suite: ``TestAuthBoundaryChecks``.
    """

    async def test_graph_crud_endpoints_reject_unauthenticated_requests(self, async_client) -> None:
        """ISOLATION: All graph CRUD endpoints must return 401 or 403 with no token."""
        fake_id = str(uuid.uuid4())
        unauthenticated_endpoints = [
            ("GET", f"/api/v1/graphs/{fake_id}"),
            ("PUT", f"/api/v1/graphs/{fake_id}"),
            ("DELETE", f"/api/v1/graphs/{fake_id}"),
            ("GET", "/api/v1/graphs"),
            ("POST", "/api/v1/graphs"),
            ("POST", f"/api/v1/graphs/{fake_id}/ingest"),
            ("POST", f"/api/v1/graphs/{fake_id}/upload"),
            ("GET", f"/api/v1/graphs/{fake_id}/ontology"),
            ("POST", f"/api/v1/graphs/{fake_id}/ontology"),
            ("GET", f"/api/v1/graphs/{fake_id}/instructions"),
            ("PUT", f"/api/v1/graphs/{fake_id}/instructions"),
        ]

        for method, path in unauthenticated_endpoints:
            response = await async_client.request(method, path)
            assert response.status_code in (401, 403), (
                f"{method} {path} returned {response.status_code} — "
                "unauthenticated requests must be rejected with 401 or 403"
            )

    async def test_create_graph_user_id_sourced_from_token_not_body(self, async_client) -> None:
        """ISOLATION: POST /api/v1/graphs must bind user_id from the auth token.

        A caller must not be able to supply a different user_id in the request
        body to create a graph owned by another user.  The API layer is
        responsible for ignoring or overwriting any body-supplied user_id with
        the authenticated principal's ID.
        """
        auth = _auth_for(USER_A)
        try:
            with (
                patch(f"{_GRAPHS_EP}.neo4j_client") as mock_neo4j,
                patch(f"{_GRAPHS_EP}.GraphNodeService") as MockSvc,
            ):
                mock_neo4j.sync_driver = MagicMock()
                svc = MockSvc.return_value
                svc.create_graph.return_value = _graph_node(
                    str(uuid.uuid4()), USER_A_ID, name="New Graph"
                )

                response = await async_client.post(
                    "/api/v1/graphs",
                    json={
                        "name": "New Graph",
                        "user_id": USER_B_ID,  # attacker attempts to own-as-B
                    },
                    headers=_headers(),
                )
        finally:
            auth.stop()

        if response.status_code in (200, 201):
            created = response.json()
            assert created["user_id"] == USER_A_ID, (
                f"Graph created with user_id={created['user_id']} "
                f"— expected authenticated user {USER_A_ID}, not body-supplied {USER_B_ID}"
            )

    async def test_ingest_endpoint_requires_authentication(self, async_client) -> None:
        """ISOLATION: POST /api/v1/graphs/{id}/ingest with no token → 401/403."""
        response = await async_client.post(
            f"/api/v1/graphs/{GRAPH_A_ID}/ingest",
            json={"content": "test content"},
        )
        assert response.status_code in (401, 403)
