"""Ontology HTTP layer (R3.5-P1-S5) — get/set with the real OntologyService + in-memory repos.
Auth (401), validation (422 bad mode / unsafe label), owner gate (404) are real route behaviour.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.core.dependencies import get_ontology_service
from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound
from oraclous_knowledge_graph_service.services.ontology_service import OntologyService

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}
_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")


class _FakeGraphRepo:
    def __init__(self) -> None:
        self.store: dict[uuid.UUID, dict] = {}

    async def get_ontology(self, graph_id):
        return self.store.get(graph_id)

    async def set_ontology(self, graph_id, ontology):
        self.store[graph_id] = ontology
        return True


class _FakeGraphService:
    def __init__(self, owned: bool = True) -> None:
        self.owned = owned

    async def get_graph(self, *, graph_id, user_id):
        if not self.owned:
            raise GraphNotFound(str(graph_id))
        now = datetime(2026, 6, 4, tzinfo=UTC)
        return Graph(
            id=graph_id,
            organisation_id=_ORG,
            user_id=user_id,
            name="g",
            description=None,
            status="active",
            node_count=0,
            relationship_count=0,
            created_at=now,
            updated_at=now,
        )


@pytest.fixture
def graph_service() -> _FakeGraphService:
    return _FakeGraphService()


@pytest.fixture
def client(app, async_client, graph_service):
    service = OntologyService(_FakeGraphRepo(), graph_service)
    app.dependency_overrides[get_ontology_service] = lambda: service
    yield async_client
    app.dependency_overrides.clear()


async def test_ontology_requires_auth(client) -> None:
    assert (await client.get(f"/api/v1/graphs/{uuid.uuid4()}/ontology")).status_code == 401


async def test_default_ontology_is_open(client) -> None:
    resp = await client.get(f"/api/v1/graphs/{uuid.uuid4()}/ontology", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"allowed_labels": [], "mode": "open"}


async def test_set_then_get_ontology(client) -> None:
    gid = uuid.uuid4()
    put = await client.put(
        f"/api/v1/graphs/{gid}/ontology",
        json={"allowed_labels": ["Person", "Org"], "mode": "strict"},
        headers=_AUTH,
    )
    assert put.status_code == 200, put.text
    got = await client.get(f"/api/v1/graphs/{gid}/ontology", headers=_AUTH)
    assert got.json() == {"allowed_labels": ["Person", "Org"], "mode": "strict"}


async def test_set_unsafe_label_is_422(client) -> None:
    resp = await client.put(
        f"/api/v1/graphs/{uuid.uuid4()}/ontology",
        json={"allowed_labels": ["__Evil__"], "mode": "strict"},
        headers=_AUTH,
    )
    assert resp.status_code == 422


async def test_set_strict_without_labels_is_422(client) -> None:
    resp = await client.put(
        f"/api/v1/graphs/{uuid.uuid4()}/ontology",
        json={"allowed_labels": [], "mode": "strict"},
        headers=_AUTH,
    )
    assert resp.status_code == 422


async def test_unowned_graph_is_404(client, graph_service) -> None:
    graph_service.owned = False
    resp = await client.get(f"/api/v1/graphs/{uuid.uuid4()}/ontology", headers=_AUTH)
    assert resp.status_code == 404
