"""HITL resolution endpoints at the HTTP layer (#279).

Exercises the REAL routes, DI wiring, dev-auth seam and error mapping. The ResolutionService is a
fake injected via `dependency_overrides[get_resolution_service]`, so no Neo4j/Postgres is needed;
the auth seam (401) and the route's error→status mapping (200/404/409/422) are real. The live merge
against a real graph is covered by the docker smoke.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_knowledge_graph_service.core.dependencies import get_resolution_service
from oraclous_knowledge_graph_service.domain.resolution import (
    CandidateNotFound,
    CandidatePair,
    MergeOutcome,
    RejectOutcome,
    ResolutionConflict,
)
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}
_GRAPH = uuid.uuid4()
_A = "node-a"
_B = "node-b"
_CID = CandidatePair(node_id_a=_A, node_id_b=_B).candidate_id


class _FakeResolutionService:
    """Configurable stand-in for ResolutionService (same async surface the routes call)."""

    def __init__(self) -> None:
        self.raise_with: Exception | None = None
        self.approve_calls: list[tuple] = []
        self.reject_calls: list[tuple] = []

    async def approve(self, *, graph_id, user_id, pair, candidate_id_path):
        self.approve_calls.append((graph_id, user_id, pair.candidate_id, candidate_id_path))
        if self.raise_with is not None:
            raise self.raise_with
        return MergeOutcome(
            survivor_id=pair.node_id_a,
            merged_id=pair.node_id_b,
            repointed_edges=3,
            aliases=["Eurail", "Eurail B.V."],
        )

    async def reject(self, *, graph_id, user_id, pair, candidate_id_path):
        self.reject_calls.append((graph_id, user_id, pair.candidate_id, candidate_id_path))
        if self.raise_with is not None:
            raise self.raise_with
        return RejectOutcome(node_id_a=pair.node_id_a, node_id_b=pair.node_id_b, suppressed=True)


@pytest.fixture
def svc() -> _FakeResolutionService:
    return _FakeResolutionService()


@pytest.fixture
def client(app, async_client, svc):
    app.dependency_overrides[get_resolution_service] = lambda: svc
    yield async_client
    app.dependency_overrides.clear()


def _body(a: str = _A, b: str = _B) -> dict:
    return {"canonical_node_id": a, "other_node_id": b}


def _url(action: str, cid: str = _CID, graph: uuid.UUID = _GRAPH) -> str:
    return f"/api/v1/graphs/{graph}/resolution/{cid}/{action}"


async def test_approve_requires_auth(client) -> None:
    resp = await client.post(_url("approve"), json=_body())
    assert resp.status_code == 401


async def test_approve_merges_and_returns_survivor(client, svc) -> None:
    resp = await client.post(_url("approve"), json=_body(), headers=_AUTH)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["survivor_id"] == _A
    assert payload["merged_id"] == _B
    assert payload["repointed_edges"] == 3
    assert payload["candidate_id"] == _CID
    assert "organisation_id" not in payload
    # the path candidate-id reached the service for the path/body match check
    assert svc.approve_calls[0][3] == _CID


async def test_reject_suppresses(client) -> None:
    resp = await client.post(_url("reject"), json=_body(), headers=_AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["suppressed"] is True


async def test_unknown_candidate_is_404(client, svc) -> None:
    svc.raise_with = CandidateNotFound(_CID)
    resp = await client.post(_url("approve"), json=_body(), headers=_AUTH)
    assert resp.status_code == 404


async def test_unowned_graph_is_404(client, svc) -> None:
    svc.raise_with = GraphNotFound(str(_GRAPH))
    resp = await client.post(_url("reject"), json=_body(), headers=_AUTH)
    assert resp.status_code == 404


async def test_conflicting_reviewer_is_409(client, svc) -> None:
    svc.raise_with = ResolutionConflict(_CID)
    resp = await client.post(_url("approve"), json=_body(), headers=_AUTH)
    assert resp.status_code == 409


async def test_identical_nodes_is_422(client) -> None:
    # node_id_a == node_id_b is a malformed pair → 422, before any service call.
    resp = await client.post(_url("approve"), json=_body(a="same", b="same"), headers=_AUTH)
    assert resp.status_code == 422


async def test_missing_body_field_is_422(client) -> None:
    resp = await client.post(_url("approve"), json={"canonical_node_id": _A}, headers=_AUTH)
    assert resp.status_code == 422
