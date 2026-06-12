"""Unit tests for AnalyticsService (#303) with fakes — no Neo4j, no LLM.

Covers the orchestration contract: detect skips below the entity floor and runs above it; list/get/
status/analytics delegate correctly; the owner gate maps a cross-org/non-owned graph to
GraphNotFound (→404, the cross-tenant denial); an unknown kind is rejected; summarise is unavailable
when no summarizer is configured.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.domain.community import (
    Community,
    CommunityMember,
)
from oraclous_knowledge_graph_service.services.analytics_service import (
    AnalyticsService,
    SummarizationUnavailable,
    UnknownCommunityKind,
)
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound

pytestmark = [pytest.mark.unit, pytest.mark.organization_isolation]

_USER = uuid.uuid4()
_GRAPH = uuid.uuid4()


class _FakeGraphService:
    """Owner gate stand-in: raises GraphNotFound for graphs the caller does not own (cross-org)."""

    def __init__(self, *, owned: set[uuid.UUID]) -> None:
        self._owned = owned

    async def get_graph(self, *, graph_id: uuid.UUID, user_id: uuid.UUID):  # noqa: ANN202, ARG002
        if graph_id not in self._owned:
            raise GraphNotFound(str(graph_id))
        return object()


class _FakeRepo:
    def __init__(
        self, *, entity_count: int = 0, communities: list[Community] | None = None
    ) -> None:
        self._entity_count = entity_count
        self._communities = communities or []
        self.detected_with: tuple[float, ...] | None = None

    def count_entities(self, *, graph_id: str) -> int:  # noqa: ARG002
        return self._entity_count

    def detect(self, *, graph_id: str, resolutions):  # noqa: ANN001, ARG002
        self.detected_with = resolutions
        # 5 levels, each one community of all entities (enough to exercise per-level counts).
        return {level: {f"c{level}": ["e1", "e2", "e3"]} for level in range(len(resolutions))}

    def list_communities(self, *, graph_id: str, level, min_entities):  # noqa: ANN001, ARG002
        if level is None:
            return self._communities
        return [c for c in self._communities if c.level == level]

    def get_community(self, *, graph_id: str, community_id: str):  # noqa: ANN001, ARG002
        return next((c for c in self._communities if c.community_id == community_id), None)

    def status(self, *, graph_id: str):  # noqa: ANN001, ARG002
        levels = sorted({c.level for c in self._communities})
        return len(self._communities), levels, self._entity_count

    def analytics(self, *, graph_id: str):  # noqa: ANN001, ARG002
        return {
            "node_count": 10,
            "relationship_count": 4,
            "entity_count": self._entity_count,
            "density": 0.04,
            "avg_degree": 0.8,
            "entity_types": [{"label": "Person", "count": 3}],
            "relationship_types": [{"type": "KNOWS", "count": 4}],
            "top_entities": [{"entity_id": "e1", "name": "Alice", "degree": 3}],
            "community_count": len(self._communities),
        }


def _svc(repo: _FakeRepo, *, owned: bool = True, summarizer=None) -> AnalyticsService:
    return AnalyticsService(
        graph_service=_FakeGraphService(owned={_GRAPH} if owned else set()),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        summarizer=summarizer,
    )


def _community(level: int = 0, cid: str = "community_abc") -> Community:
    return Community(
        community_id=cid,
        kind="entity",
        level=level,
        resolution=1.0,
        entity_count=3,
        status="active",
        members=[CommunityMember(entity_id="e1", entity_name="Alice", entity_type="Person")],
    )


async def test_detect_skips_below_floor() -> None:
    svc = _svc(_FakeRepo(entity_count=1))
    result = await svc.detect(graph_id=_GRAPH, user_id=_USER, min_entities=3)
    assert result.status == "skipped"
    assert result.total_communities == 0


async def test_detect_runs_all_five_resolutions() -> None:
    repo = _FakeRepo(entity_count=50)
    svc = _svc(repo)
    result = await svc.detect(graph_id=_GRAPH, user_id=_USER)
    assert result.status == "completed"
    assert len(repo.detected_with) == 5  # the 5-level multi-resolution hierarchy
    assert set(result.communities_per_level) == {0, 1, 2, 3, 4}


async def test_owner_gate_blocks_cross_org_detect() -> None:
    # A graph the caller does not own (e.g. another org's) → GraphNotFound (→404, no leak).
    svc = _svc(_FakeRepo(entity_count=50), owned=False)
    with pytest.raises(GraphNotFound):
        await svc.detect(graph_id=_GRAPH, user_id=_USER)


async def test_list_and_get_delegate() -> None:
    repo = _FakeRepo(communities=[_community(level=0), _community(level=1, cid="community_xyz")])
    svc = _svc(repo)
    all_comms = await svc.list_communities(graph_id=_GRAPH, user_id=_USER)
    assert len(all_comms) == 2
    level_0 = await svc.list_communities(graph_id=_GRAPH, user_id=_USER, level=0)
    assert len(level_0) == 1
    got = await svc.get_community(graph_id=_GRAPH, user_id=_USER, community_id="community_xyz")
    assert got is not None and got.community_id == "community_xyz"
    missing = await svc.get_community(graph_id=_GRAPH, user_id=_USER, community_id="nope")
    assert missing is None


async def test_unknown_kind_rejected() -> None:
    svc = _svc(_FakeRepo(communities=[_community()]))
    with pytest.raises(UnknownCommunityKind):
        await svc.list_communities(graph_id=_GRAPH, user_id=_USER, kind="not_a_kind")


async def test_status_not_detected_then_active() -> None:
    empty = _svc(_FakeRepo(entity_count=5))
    status = await empty.status(graph_id=_GRAPH, user_id=_USER)
    assert status.status == "not_detected"
    assert status.communities_count == 0

    detected = _svc(_FakeRepo(entity_count=5, communities=[_community(level=0)]))
    status = await detected.status(graph_id=_GRAPH, user_id=_USER)
    assert status.status == "active"
    assert status.communities_count == 1
    # entity_count (5) > entities covered by level-0 communities (3) → stale.
    assert status.is_stale is True


async def test_analytics_shape() -> None:
    svc = _svc(_FakeRepo(entity_count=8, communities=[_community()]))
    a = await svc.analytics(graph_id=_GRAPH, user_id=_USER)
    assert a.node_count == 10
    assert a.relationship_count == 4
    assert a.entity_count == 8
    assert a.community_count == 1
    assert a.entity_types[0]["label"] == "Person"
    assert isinstance(a.computed_at, datetime)
    assert a.computed_at.tzinfo == UTC


async def test_summarize_unavailable_without_summarizer() -> None:
    svc = _svc(_FakeRepo(communities=[_community()]), summarizer=None)
    with pytest.raises(SummarizationUnavailable):
        await svc.summarize(graph_id=_GRAPH, user_id=_USER)
