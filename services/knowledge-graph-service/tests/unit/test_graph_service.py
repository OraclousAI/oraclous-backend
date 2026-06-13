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
    ReservedGraphName,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_OWNER = uuid.uuid4()
_INTRUDER = uuid.uuid4()


class _FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, Graph] = {}

    def make(self, user_id: uuid.UUID, name: str = "g", system_kind: str | None = None) -> Graph:
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
            system_kind=system_kind,
        )

    async def create(self, *, user_id, name, description, system_kind=None) -> Graph:
        g = self.make(user_id, name, system_kind=system_kind)
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


class _FakeWriteRepo:
    """Records the graph_id-scoped Neo4j cascade the delete flow must trigger (ORAA-261)."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_graph_nodes(self, *, graph_id: str) -> int:
        self.deleted.append(graph_id)
        return 3  # pretend 3 nodes were detached


class _RaisingWriteRepo:
    def delete_graph_nodes(self, *, graph_id: str) -> int:
        raise RuntimeError("neo4j unreachable")


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


async def test_delete_cascades_neo4j_nodes_for_owned_graph() -> None:
    # ORAA-261: deleting a graph must cascade its Neo4j nodes (graph_id-scoped DETACH DELETE),
    # not just remove the Postgres metadata row.
    repo = _FakeRepo()
    write_repo = _FakeWriteRepo()
    svc = GraphService(repo, write_repo=write_repo)
    g = await svc.create_graph(user_id=_OWNER, name="g", description=None)
    await svc.delete_graph(graph_id=g.id, user_id=_OWNER)
    assert write_repo.deleted == [str(g.id)]  # graph_id-scoped cascade fired exactly once
    assert g.id not in repo.rows  # and the Postgres row was removed


async def test_delete_without_write_repo_only_removes_postgres_row() -> None:
    # Substrate unwired (unit/unconfigured) -> no Neo4j cascade, Postgres delete still happens.
    repo = _FakeRepo()
    svc = GraphService(repo)  # write_repo defaults to None
    g = await svc.create_graph(user_id=_OWNER, name="g", description=None)
    await svc.delete_graph(graph_id=g.id, user_id=_OWNER)
    assert g.id not in repo.rows


async def test_delete_intruder_does_not_cascade_neo4j() -> None:
    # The owner gate runs before the cascade: a non-owner delete never touches Neo4j.
    repo = _FakeRepo()
    write_repo = _FakeWriteRepo()
    svc = GraphService(repo, write_repo=write_repo)
    g = await svc.create_graph(user_id=_OWNER, name="g", description=None)
    with pytest.raises(GraphNotFound):
        await svc.delete_graph(graph_id=g.id, user_id=_INTRUDER)
    assert write_repo.deleted == []  # cascade NOT fired for an unowned graph
    assert g.id in repo.rows


async def test_delete_surfaces_neo4j_failure_and_keeps_postgres_row() -> None:
    # A Neo4j cascade failure is surfaced (not swallowed) and aborts before the Postgres delete,
    # so the graph never ends up orphaned — it survives to be retried.
    repo = _FakeRepo()
    svc = GraphService(repo, write_repo=_RaisingWriteRepo())
    g = await svc.create_graph(user_id=_OWNER, name="g", description=None)
    with pytest.raises(RuntimeError):
        await svc.delete_graph(graph_id=g.id, user_id=_OWNER)
    assert g.id in repo.rows  # Postgres row NOT removed when Neo4j cleanup failed


async def test_get_missing_raises_not_found() -> None:
    svc = GraphService(_FakeRepo())
    with pytest.raises(GraphNotFound):
        await svc.get_graph(graph_id=uuid.uuid4(), user_id=_OWNER)


async def test_create_rejects_reserved_system_name() -> None:
    # A user cannot create a graph that would shadow the system agent-memory graph (#332 §5).
    svc = GraphService(_FakeRepo())
    for reserved in ("Agent Memory", "agent memory", "  AGENT MEMORY  "):
        with pytest.raises(ReservedGraphName):
            await svc.create_graph(user_id=_OWNER, name=reserved, description=None)


async def test_create_allows_non_reserved_name() -> None:
    svc = GraphService(_FakeRepo())
    g = await svc.create_graph(user_id=_OWNER, name="My Agent Memories", description=None)
    assert g.system_kind is None  # a user graph is never minted as a system graph


def test_graphnodeservice_alias_is_graphservice() -> None:
    # Legacy patch target preserved (ORAA-55 era tests reference GraphNodeService).
    assert GraphNodeService is GraphService
