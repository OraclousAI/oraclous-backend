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
    CrossGraphCandidate,
    LinkOutcome,
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
        self.generate_calls: list[tuple] = []

    async def approve(self, *, graph_id, user_id, pair, candidate_id_path, other_graph_id=None):
        self.approve_calls.append(
            (graph_id, user_id, pair.candidate_id, candidate_id_path, other_graph_id)
        )
        if self.raise_with is not None:
            raise self.raise_with
        if other_graph_id is not None and other_graph_id != graph_id:
            return LinkOutcome(
                node_id_a=pair.node_id_a,
                node_id_b=pair.node_id_b,
                graph_id_a=str(graph_id),
                graph_id_b=str(other_graph_id),
                linked=True,
            )
        return MergeOutcome(
            survivor_id=pair.node_id_a,
            merged_id=pair.node_id_b,
            repointed_edges=3,
            aliases=["Eurail", "Eurail B.V."],
        )

    async def reject(self, *, graph_id, user_id, pair, candidate_id_path, other_graph_id=None):
        self.reject_calls.append(
            (graph_id, user_id, pair.candidate_id, candidate_id_path, other_graph_id)
        )
        if self.raise_with is not None:
            raise self.raise_with
        return RejectOutcome(node_id_a=pair.node_id_a, node_id_b=pair.node_id_b, suppressed=True)

    async def generate_cross_graph(
        self, *, graph_id, target_graph_id, user_id, candidate_threshold, limit
    ):
        self.generate_calls.append((graph_id, target_graph_id, candidate_threshold, limit))
        if self.raise_with is not None:
            raise self.raise_with
        if target_graph_id == graph_id:
            raise ValueError("cross-graph generation needs two distinct graphs")
        return (
            [
                CrossGraphCandidate(
                    node_id_a=_A,
                    node_id_b=_B,
                    graph_id_a=str(graph_id),
                    graph_id_b=str(target_graph_id),
                    label="Company",
                    name_a="Eurail",
                    name_b="Eurail B.V.",
                    score=1.0,
                    method="canonical_key",
                )
            ],
            [],
        )


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


# --- cross-graph SAME_AS (#330 / ADR-026) ---------------------------------------------------------

_OTHER_GRAPH = uuid.uuid4()


async def test_cross_graph_approve_links_instead_of_folding(client, svc) -> None:
    body = dict(_body(), other_graph_id=str(_OTHER_GRAPH))
    resp = await client.post(_url("approve"), json=body, headers=_AUTH)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["linked"] is True
    assert payload["survivor_id"] == _A and payload["merged_id"] == _B
    assert payload["repointed_edges"] == 0 and payload["aliases"] == []
    # the other graph id reached the service (both graphs get owner-gated there)
    assert svc.approve_calls[0][4] == _OTHER_GRAPH


async def test_in_graph_approve_response_is_unchanged(client) -> None:
    resp = await client.post(_url("approve"), json=_body(), headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["linked"] is False  # additive field; the merge contract is intact


async def test_cross_graph_reject_passes_the_other_graph(client, svc) -> None:
    body = dict(_body(), other_graph_id=str(_OTHER_GRAPH))
    resp = await client.post(_url("reject"), json=body, headers=_AUTH)
    assert resp.status_code == 200
    assert svc.reject_calls[0][4] == _OTHER_GRAPH


async def test_generate_cross_graph_requires_auth(client) -> None:
    resp = await client.post(
        f"/api/v1/graphs/{_GRAPH}/resolution/cross-graph",
        json={"target_graph_id": str(_OTHER_GRAPH)},
    )
    assert resp.status_code == 401


async def test_generate_cross_graph_returns_the_review_queue(client, svc) -> None:
    resp = await client.post(
        f"/api/v1/graphs/{_GRAPH}/resolution/cross-graph",
        json={"target_graph_id": str(_OTHER_GRAPH)},
        headers=_AUTH,
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["generated"] == 1
    cand = payload["candidates"][0]
    # BOTH graph ids carried on the candidate (ADR-026) + the stable pair id for the verdicts
    assert cand["graph_id_a"] == str(_GRAPH) and cand["graph_id_b"] == str(_OTHER_GRAPH)
    assert cand["candidate_id"] == _CID
    assert cand["method"] == "canonical_key"
    # defaults flowed through
    assert svc.generate_calls[0][2] == 0.85 and svc.generate_calls[0][3] == 100


async def test_generate_against_itself_is_422(client, svc) -> None:
    resp = await client.post(
        f"/api/v1/graphs/{_GRAPH}/resolution/cross-graph",
        json={"target_graph_id": str(_GRAPH)},
        headers=_AUTH,
    )
    assert resp.status_code == 422


async def test_generate_unowned_graph_is_404(client, svc) -> None:
    svc.raise_with = GraphNotFound(str(_OTHER_GRAPH))
    resp = await client.post(
        f"/api/v1/graphs/{_GRAPH}/resolution/cross-graph",
        json={"target_graph_id": str(_OTHER_GRAPH)},
        headers=_AUTH,
    )
    assert resp.status_code == 404
