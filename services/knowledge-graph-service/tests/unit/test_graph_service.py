"""GraphService use-case logic (R3.5-P1-S1) — owner gate, in isolation from HTTP/DB.

The org scope is enforced in the repository (covered by test_graph_repository_failclosed + the
docker smoke); here we prove the per-user ownership gate the service layer adds on top.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.services.graph_service import (
    GraphNodeService,
    GraphNotFound,
    GraphService,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_OWNER = uuid.uuid4()
_INTRUDER = uuid.uuid4()


class _FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, Graph] = {}

    def make(self, user_id: uuid.UUID, name: str = "g") -> Graph:
        now = datetime(2026, 6, 4, tzinfo=UTC)
        return Graph(
            id=uuid.uuid4(),
            organisation_id=_ORG,
            user_id=user_id,
            name=name,
            description=None,
            status="active",
            node_count=0,
            relationship_count=0,
            created_at=now,
            updated_at=now,
        )

    async def create(self, *, user_id, name, description) -> Graph:
        g = self.make(user_id, name)
        self.rows[g.id] = g
        return g

    async def list_for_user(self, *, user_id) -> list[Graph]:
        return [g for g in self.rows.values() if g.user_id == user_id]

    async def get(self, graph_id) -> Graph | None:
        return self.rows.get(graph_id)

    async def update(self, graph_id, *, name, description) -> Graph | None:
        g = self.rows.get(graph_id)
        return g  # name/description mutation irrelevant to the gate tests

    async def delete(self, graph_id) -> bool:
        return self.rows.pop(graph_id, None) is not None


async def test_create_and_get_own() -> None:
    svc = GraphService(_FakeRepo())
    g = await svc.create_graph(user_id=_OWNER, name="g", description=None)
    fetched = await svc.get_graph(graph_id=g.id, user_id=_OWNER)
    assert fetched.id == g.id


async def test_get_other_owner_raises_not_found() -> None:
    repo = _FakeRepo()
    svc = GraphService(repo)
    g = await svc.create_graph(user_id=_OWNER, name="g", description=None)
    with pytest.raises(GraphNotFound):
        await svc.get_graph(graph_id=g.id, user_id=_INTRUDER)


async def test_update_other_owner_raises_and_does_not_mutate() -> None:
    repo = _FakeRepo()
    svc = GraphService(repo)
    g = await svc.create_graph(user_id=_OWNER, name="g", description=None)
    with pytest.raises(GraphNotFound):
        await svc.update_graph(graph_id=g.id, user_id=_INTRUDER, name="hijacked", description=None)


async def test_delete_other_owner_raises() -> None:
    repo = _FakeRepo()
    svc = GraphService(repo)
    g = await svc.create_graph(user_id=_OWNER, name="g", description=None)
    with pytest.raises(GraphNotFound):
        await svc.delete_graph(graph_id=g.id, user_id=_INTRUDER)
    assert g.id in repo.rows


async def test_get_missing_raises_not_found() -> None:
    svc = GraphService(_FakeRepo())
    with pytest.raises(GraphNotFound):
        await svc.get_graph(graph_id=uuid.uuid4(), user_id=_OWNER)


def test_graphnodeservice_alias_is_graphservice() -> None:
    # Legacy patch target preserved (ORAA-55 era tests reference GraphNodeService).
    assert GraphNodeService is GraphService
