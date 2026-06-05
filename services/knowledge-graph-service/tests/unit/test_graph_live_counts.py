"""GraphService live-count overlay: node_count/relationship_count are read live from Neo4j
(org+graph scoped). Covers the overlay, the write_repo-None fallback (Postgres columns), and the
degrade-don't-crash fallback when the Neo4j count raises mid-request (must not 500 a metadata read).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.services.graph_service import GraphService

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_OWNER = uuid.uuid4()


def _graph(*, node_count: int = 0, relationship_count: int = 0) -> Graph:
    now = datetime(2026, 6, 4, tzinfo=UTC)
    return Graph(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_OWNER,
        name="g",
        description=None,
        status="active",
        node_count=node_count,
        relationship_count=relationship_count,
        created_at=now,
        updated_at=now,
    )


class _CountingWriteRepo:
    def __init__(self, result: tuple[int, int] = (5, 4)) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def count_for_graph(self, *, graph_id: str, organisation_id: str) -> tuple[int, int]:
        self.calls.append((graph_id, organisation_id))
        return self.result


class _RaisingWriteRepo:
    def count_for_graph(self, *, graph_id: str, organisation_id: str) -> tuple[int, int]:
        raise RuntimeError("neo4j unreachable")


class _ListRepo:
    def __init__(self, graphs: list[Graph]) -> None:
        self._graphs = graphs

    async def list_for_user(self, *, user_id: uuid.UUID) -> list[Graph]:
        return list(self._graphs)


async def test_overlays_live_counts_when_write_repo_present() -> None:
    graph = _graph()
    write_repo = _CountingWriteRepo((5, 4))
    service = GraphService(repo=None, write_repo=write_repo)  # type: ignore[arg-type]
    out = await service._with_live_counts(graph)
    assert out.node_count == 5
    assert out.relationship_count == 4
    # org+graph scoped: the live count is invoked with THIS graph's id + org (no cross-tenant read)
    assert write_repo.calls == [(str(graph.id), str(_ORG))]


async def test_falls_back_to_stored_counts_without_write_repo() -> None:
    graph = _graph(node_count=7, relationship_count=3)
    service = GraphService(repo=None, write_repo=None)  # type: ignore[arg-type]
    out = await service._with_live_counts(graph)
    assert out.node_count == 7
    assert out.relationship_count == 3


async def test_degrades_to_stored_counts_when_neo4j_count_raises() -> None:
    graph = _graph(node_count=9, relationship_count=2)
    service = GraphService(repo=None, write_repo=_RaisingWriteRepo())  # type: ignore[arg-type]
    out = await service._with_live_counts(graph)  # must NOT raise
    assert out.node_count == 9
    assert out.relationship_count == 2


async def test_list_graphs_isolates_a_failing_count() -> None:
    graph = _graph()
    service = GraphService(repo=_ListRepo([graph]), write_repo=_RaisingWriteRepo())  # type: ignore[arg-type]
    out = await service.list_graphs(user_id=_OWNER)
    assert len(out) == 1
    assert out[0].node_count == 0  # degraded to stored, did not crash the whole list
