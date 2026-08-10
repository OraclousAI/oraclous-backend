"""Graph CRUD + isolation at the HTTP layer (R3.5-P1-S1).

Exercises the REAL routes, DI wiring, dev-auth seam and error mapping. The persistence layer is an
in-memory fake injected via `dependency_overrides[get_graph_service]`, so no Postgres is needed; the
gates and the auth seam (401) are real. Live cross-org scoping (`enforced_organisation_id`) is
verified by the docker smoke.

#736 / ADR-051: reads and writes no longer share one gate. Read is ORG-scoped — a member sees and
opens every workspace in their organisation — and write stays OWNER-scoped. The 404-no-leak
assertions therefore moved from "another user" to "another organisation", which is where the wall
now is. The fake repository below models the org filter the real one applies, so a graph outside the
bound organisation is invisible rather than merely forbidden.

Replaces the hollow-era `test_api_authz_isolation.py`, which patched the now-deleted inline
`GraphNodeService` stub and the `api.*` modules.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.core.dependencies import get_graph_service
from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound, GraphService

pytestmark = pytest.mark.integration

_DEV_USER = uuid.UUID("00000000-0000-0000-0000-0000000000d5")  # matches Settings.dev_user_id
_DEV_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")  # matches Settings.dev_org_id
_OTHER_USER = uuid.UUID("00000000-0000-0000-0000-00000000beef")  # same org, a colleague
_OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005ff")  # a different tenant
_AUTH = {"Authorization": "Bearer dev-token"}


class _FakeRepo:
    """In-memory stand-in for GraphRepository (same method surface GraphService calls).

    Reads are filtered by the bound organisation, mirroring `enforced_organisation_id()`: a row in
    another tenant is invisible, never merely unowned."""

    def __init__(self) -> None:
        self._rows: dict[uuid.UUID, Graph] = {}

    def _make(
        self,
        *,
        user_id: uuid.UUID,
        name: str,
        description: str | None,
        organisation_id: uuid.UUID = _DEV_ORG,
    ) -> Graph:
        now = datetime(2026, 6, 4, tzinfo=UTC)
        return Graph(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            name=name,
            description=description,
            status="active",
            node_count=0,
            relationship_count=0,
            created_at=now,
            updated_at=now,
        )

    def seed(self, graph: Graph) -> None:
        self._rows[graph.id] = graph

    async def create(self, *, user_id: uuid.UUID, name: str, description: str | None) -> Graph:
        g = self._make(user_id=user_id, name=name, description=description)
        self._rows[g.id] = g
        return g

    async def list_for_org(self) -> list[Graph]:
        return [g for g in self._rows.values() if g.organisation_id == _DEV_ORG]

    async def get(self, graph_id: uuid.UUID) -> Graph | None:
        g = self._rows.get(graph_id)
        return g if g is not None and g.organisation_id == _DEV_ORG else None

    async def update(
        self,
        graph_id: uuid.UUID,
        *,
        name: str | None,
        description: str | None,
        model_credential_id: str | None = None,
    ) -> Graph | None:
        g = self._rows.get(graph_id)
        if g is None:
            return None
        updated = Graph(
            id=g.id,
            organisation_id=g.organisation_id,
            user_id=g.user_id,
            name=name if name is not None else g.name,
            description=description if description is not None else g.description,
            status=g.status,
            node_count=g.node_count,
            relationship_count=g.relationship_count,
            created_at=g.created_at,
            updated_at=g.updated_at,
        )
        self._rows[graph_id] = updated
        return updated

    async def delete(self, graph_id: uuid.UUID) -> bool:
        return self._rows.pop(graph_id, None) is not None


@pytest.fixture
def repo() -> _FakeRepo:
    return _FakeRepo()


@pytest.fixture
def client(app, async_client, repo):
    # The real owner gate lives in GraphService; we back it with the fake repo (no DB).
    app.dependency_overrides[get_graph_service] = lambda: GraphService(repo)
    yield async_client
    app.dependency_overrides.clear()


async def test_missing_token_is_401(client) -> None:
    resp = await client.get("/api/v1/graphs")
    assert resp.status_code == 401


async def test_wrong_token_is_401(client) -> None:
    resp = await client.get("/api/v1/graphs", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


async def test_create_then_get_roundtrip(client) -> None:
    created = await client.post("/api/v1/graphs", json={"name": "g1"}, headers=_AUTH)
    assert created.status_code == 201, created.text
    gid = created.json()["id"]
    # organisation_id must never appear in the response (resolved from context, not echoed).
    assert "organisation_id" not in created.json()

    got = await client.get(f"/api/v1/graphs/{gid}", headers=_AUTH)
    assert got.status_code == 200
    assert got.json()["name"] == "g1"


async def test_list_returns_the_organisations_graphs(client, repo) -> None:
    """#736 / ADR-051 acceptance 1. The list gate now equals the read gate, so a colleague's
    workspace appears instead of being hidden by a check that never protected it."""
    await client.post("/api/v1/graphs", json={"name": "mine"}, headers=_AUTH)
    repo.seed(repo._make(user_id=_OTHER_USER, name="theirs", description=None))
    resp = await client.get("/api/v1/graphs", headers=_AUTH)
    assert resp.status_code == 200
    assert {g["name"] for g in resp.json()} == {"mine", "theirs"}


async def test_list_marks_which_graphs_the_caller_owns(client, repo) -> None:
    """Acceptance 1's second half: ownership is visible, so the console can separate "mine" from
    "the organisation's". Derived from the graph's own `user_id` — no extra query, nothing
    stored."""
    await client.post("/api/v1/graphs", json={"name": "mine"}, headers=_AUTH)
    repo.seed(repo._make(user_id=_OTHER_USER, name="theirs", description=None))
    rows = {g["name"]: g for g in (await client.get("/api/v1/graphs", headers=_AUTH)).json()}
    assert rows["mine"]["is_owner"] is True
    assert rows["theirs"]["is_owner"] is False


async def test_list_excludes_another_organisation(client, repo) -> None:
    """Acceptance 4. The org boundary is the wall, and it did not move."""
    await client.post("/api/v1/graphs", json={"name": "mine"}, headers=_AUTH)
    repo.seed(
        repo._make(
            user_id=_OTHER_USER,
            name="RivalCorpKB",
            description="confidential",
            organisation_id=_OTHER_ORG,
        )
    )
    resp = await client.get("/api/v1/graphs", headers=_AUTH)
    assert resp.status_code == 200
    assert {g["name"] for g in resp.json()} == {"mine"}
    assert "RivalCorpKB" not in resp.text


async def test_get_a_colleagues_graph_in_the_same_org_is_200(client, repo) -> None:
    """The boundary ruled on #736: graph detail is a pure read, so it follows the list. A member who
    sees a row in the list can open it — the console never renders a row that then 404s."""
    theirs = repo._make(user_id=_OTHER_USER, name="Company Knowledge Base", description=None)
    repo.seed(theirs)
    resp = await client.get(f"/api/v1/graphs/{theirs.id}", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Company Knowledge Base"
    assert resp.json()["is_owner"] is False


async def test_get_a_graph_in_another_org_is_404_no_leak(client, repo) -> None:
    """Acceptance 4 on the detail route. The 404-no-leak guarantee moved to the org boundary; it was
    not dropped."""
    secret = repo._make(
        user_id=_OTHER_USER,
        name="RivalCorpSecret",
        description="confidential",
        organisation_id=_OTHER_ORG,
    )
    repo.seed(secret)
    resp = await client.get(f"/api/v1/graphs/{secret.id}", headers=_AUTH)
    assert resp.status_code == 404
    assert "RivalCorpSecret" not in resp.text
    assert "confidential" not in resp.text


async def test_update_other_users_graph_is_404(client, repo) -> None:
    """Acceptance 3. Read widened; rename did not. The caller can open this graph one route over and
    is still refused the write — ADR-051 decision 3."""
    secret = repo._make(user_id=_OTHER_USER, name="theirs", description=None)
    repo.seed(secret)
    assert (await client.get(f"/api/v1/graphs/{secret.id}", headers=_AUTH)).status_code == 200
    resp = await client.patch(
        f"/api/v1/graphs/{secret.id}", json={"name": "hijacked"}, headers=_AUTH
    )
    assert resp.status_code == 404
    # and the row is untouched
    assert repo._rows[secret.id].name == "theirs"


async def test_delete_other_users_graph_is_404(client, repo) -> None:
    """Acceptance 3. Delete stays owner-scoped."""
    secret = repo._make(user_id=_OTHER_USER, name="theirs", description=None)
    repo.seed(secret)
    resp = await client.delete(f"/api/v1/graphs/{secret.id}", headers=_AUTH)
    assert resp.status_code == 404
    assert secret.id in repo._rows  # not deleted


async def test_update_then_delete_own_graph(client) -> None:
    gid = (await client.post("/api/v1/graphs", json={"name": "g"}, headers=_AUTH)).json()["id"]
    upd = await client.patch(f"/api/v1/graphs/{gid}", json={"name": "g2"}, headers=_AUTH)
    assert upd.status_code == 200 and upd.json()["name"] == "g2"
    deleted = await client.delete(f"/api/v1/graphs/{gid}", headers=_AUTH)
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/graphs/{gid}", headers=_AUTH)).status_code == 404


async def test_get_unknown_graph_is_404(client) -> None:
    resp = await client.get(f"/api/v1/graphs/{uuid.uuid4()}", headers=_AUTH)
    assert resp.status_code == 404


async def test_create_rejects_empty_name(client) -> None:
    resp = await client.post("/api/v1/graphs", json={"name": ""}, headers=_AUTH)
    assert resp.status_code == 422  # pydantic min_length=1


def test_graphnotfound_is_importable_for_patchers() -> None:
    # GraphNotFound is the documented owner-gate error the routes map to 404.
    assert issubclass(GraphNotFound, Exception)
