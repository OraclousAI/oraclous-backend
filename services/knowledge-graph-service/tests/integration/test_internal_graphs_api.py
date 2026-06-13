"""Internal accessible-set HTTP layer (#330 / ADR-026) — GET /internal/v1/graphs.

The federation enumeration seam the knowledge-retriever calls before a fan-out. No Postgres:
``get_graph_service`` is overridden with a fake. The decisive checks: auth is required (401 with
no bearer — gateway-mode X-Internal-Key gating is the same ``get_principal`` covered in
tests/unit/test_gateway_mode_auth.py); the response is the org's graphs (id + name) — the
ORG-scoped list, NOT the per-user one — exactly the set the retriever's single-graph reads
already admit; and the body carries no organisation_id (ORG001).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.core.dependencies import get_graph_service
from oraclous_knowledge_graph_service.domain.graph import Graph

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}
_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_G1 = uuid.uuid4()
_G2 = uuid.uuid4()


def _graph(graph_id: uuid.UUID, name: str, owner: uuid.UUID) -> Graph:
    now = datetime(2026, 6, 12, tzinfo=UTC)
    return Graph(
        id=graph_id,
        organisation_id=_ORG,
        user_id=owner,
        name=name,
        description=None,
        status="active",
        node_count=0,
        relationship_count=0,
        created_at=now,
        updated_at=now,
    )


class _FakeGraphService:
    def __init__(self) -> None:
        # TWO different owners in ONE org: the org-scoped list returns both (the federation
        # accessible-set is org-scoped, mirroring the retriever's read gate — not owner-gated).
        self.org_graphs = [
            _graph(_G1, "research", uuid.uuid4()),
            _graph(_G2, "sales", uuid.uuid4()),
        ]

    async def list_org_graphs(self) -> list[Graph]:
        return list(self.org_graphs)


@pytest.fixture
def client(app, async_client):
    app.dependency_overrides[get_graph_service] = lambda: _FakeGraphService()
    yield async_client
    app.dependency_overrides.clear()


async def test_internal_graphs_requires_auth(client) -> None:
    resp = await client.get("/internal/v1/graphs")
    assert resp.status_code == 401


async def test_internal_graphs_returns_the_org_set_with_ids_and_names(client) -> None:
    resp = await client.get("/internal/v1/graphs", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"graphs"}
    assert body["graphs"] == [
        {"id": str(_G1), "name": "research"},
        {"id": str(_G2), "name": "sales"},
    ]
    # ORG001: the org id is scope, never a response field
    assert "organisation_id" not in resp.text
