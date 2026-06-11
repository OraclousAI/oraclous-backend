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
    body = resp.json()
    # core labels-only contract preserved (the typed/hint keys default to empty/None)
    assert body["allowed_labels"] == [] and body["mode"] == "open"
    assert body["entity_types"] == [] and body["relationship_types"] == []


async def test_set_then_get_ontology(client) -> None:
    gid = uuid.uuid4()
    put = await client.put(
        f"/api/v1/graphs/{gid}/ontology",
        json={"allowed_labels": ["Person", "Org"], "mode": "strict"},
        headers=_AUTH,
    )
    assert put.status_code == 200, put.text
    got = await client.get(f"/api/v1/graphs/{gid}/ontology", headers=_AUTH)
    body = got.json()
    # a labels-only set round-trips its core fields; no typed defs are invented
    assert body["allowed_labels"] == ["Person", "Org"] and body["mode"] == "strict"
    assert body["entity_types"] == [] and body["relationship_types"] == []


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


# --- Slice B: the typed ontology shape over the HTTP layer --------------------
async def test_set_then_get_typed_ontology(client) -> None:
    gid = uuid.uuid4()
    body = {
        "mode": "strict",
        "entity_types": [
            {"name": "Person", "description": "a human", "properties": ["name"]},
            {"name": "Company"},
        ],
        "relationship_types": [{"name": "WORKS_AT", "source": "Person", "target": "Company"}],
        "domain": "HR",
        "density": "dense",
        "focus": ["org charts"],
        "ignore": ["footers"],
    }
    put = await client.put(f"/api/v1/graphs/{gid}/ontology", json=body, headers=_AUTH)
    assert put.status_code == 200, put.text
    data = put.json()
    # allowed_labels DERIVED from entity_types
    assert data["allowed_labels"] == ["Person", "Company"]
    assert data["entity_types"][0]["name"] == "Person"
    assert data["relationship_types"][0]["source"] == "Person"
    assert data["domain"] == "HR" and data["density"] == "dense"

    got = await client.get(f"/api/v1/graphs/{gid}/ontology", headers=_AUTH)
    assert got.json() == data  # round-trips


async def test_typed_ontology_rejects_unsafe_entity_type_name(client) -> None:
    resp = await client.put(
        f"/api/v1/graphs/{uuid.uuid4()}/ontology",
        json={"mode": "strict", "entity_types": [{"name": "__Evil__"}]},
        headers=_AUTH,
    )
    assert resp.status_code == 422


async def test_typed_ontology_rejects_relationship_to_undefined_type(client) -> None:
    resp = await client.put(
        f"/api/v1/graphs/{uuid.uuid4()}/ontology",
        json={
            "mode": "strict",
            "entity_types": [{"name": "Person"}],
            "relationship_types": [{"name": "WORKS_AT", "source": "Person", "target": "Ghost"}],
        },
        headers=_AUTH,
    )
    assert resp.status_code == 422
