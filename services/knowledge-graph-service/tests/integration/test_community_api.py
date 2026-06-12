"""Community + analytics endpoints at the HTTP layer (#303).

Exercises the REAL routes, DI wiring, dev-auth seam, and error→status mapping. The AnalyticsService
is a fake injected via ``dependency_overrides[get_analytics_service]``, so no Neo4j/Postgres is
needed; the auth seam (401) and the route mapping (200/202/400/404/503) are real. The live GDS run
is covered by ``test_community_gds.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.core.dependencies import get_analytics_service
from oraclous_knowledge_graph_service.domain.community import (
    CommunitiesStatus,
    Community,
    CommunityMember,
    DetectionResult,
    GdsUnavailableError,
    GraphAnalytics,
    entity_kinds,
)
from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord
from oraclous_knowledge_graph_service.services.analytics_service import (
    SummarizationUnavailable,
    UnknownCommunityKind,
)
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}
_GRAPH = uuid.uuid4()


class _FakeAnalyticsService:
    def __init__(self) -> None:
        self.raise_with: Exception | None = None
        self.detect_returns: tuple = (None, None)
        self.communities: list[Community] = []
        self.community: Community | None = None
        self.summarized = 0

    @staticmethod
    def kinds():
        return entity_kinds()

    async def submit_detect(self, *, graph_id, user_id, min_entities, force_rebuild=False):  # noqa: ARG002
        if self.raise_with is not None:
            raise self.raise_with
        return self.detect_returns

    async def list_communities(self, *, graph_id, user_id, level=None, kind="entity"):  # noqa: ARG002
        if self.raise_with is not None:
            raise self.raise_with
        return self.communities

    async def get_community(self, *, graph_id, user_id, community_id):  # noqa: ARG002
        if self.raise_with is not None:
            raise self.raise_with
        return self.community

    async def status(self, *, graph_id, user_id):  # noqa: ARG002
        if self.raise_with is not None:
            raise self.raise_with
        return CommunitiesStatus(
            graph_id=str(graph_id),
            status="active",
            communities_count=3,
            levels=[0, 1],
            entity_count=10,
            entity_count_at_detection=10,
            is_stale=False,
        )

    async def summarize(self, *, graph_id, user_id, level=None, force=False):  # noqa: ARG002
        if self.raise_with is not None:
            raise self.raise_with
        return self.summarized

    async def analytics(self, *, graph_id, user_id):  # noqa: ARG002
        if self.raise_with is not None:
            raise self.raise_with
        return GraphAnalytics(
            graph_id=str(graph_id),
            node_count=10,
            relationship_count=4,
            entity_count=8,
            density=0.04,
            avg_degree=0.8,
            entity_types=[{"label": "Person", "count": 3}],
            relationship_types=[{"type": "KNOWS", "count": 4}],
            top_entities=[{"entity_id": "e1", "name": "Alice", "degree": 3}],
            community_count=3,
            computed_at=datetime.now(UTC),
        )


@pytest.fixture
def svc() -> _FakeAnalyticsService:
    return _FakeAnalyticsService()


@pytest.fixture
def client(app, async_client, svc):
    app.dependency_overrides[get_analytics_service] = lambda: svc
    yield async_client
    app.dependency_overrides.clear()


def _community(cid: str = "community_abc", level: int = 0) -> Community:
    return Community(
        community_id=cid,
        kind="entity",
        level=level,
        entity_count=3,
        status="active",
        summary="A friendship cluster of people.",
        summary_keywords=["Alice", "Bob"],
        summary_model="test-model",
        summary_source="llm",
        members=[CommunityMember(entity_id="e1", entity_name="Alice", entity_type="Person")],
    )


async def test_kinds_requires_auth(client) -> None:
    assert (await client.get("/api/v1/communities/kinds")).status_code == 401


async def test_kinds_lists_entity_kind(client) -> None:
    resp = await client.get("/api/v1/communities/kinds", headers=_AUTH)
    assert resp.status_code == 200
    kinds = resp.json()
    assert any(k["kind"] == "entity" and k["detection_supported"] for k in kinds)


async def test_detect_sync_returns_200_result(client, svc) -> None:
    svc.detect_returns = (
        None,
        DetectionResult(
            graph_id=str(_GRAPH),
            status="completed",
            total_communities=5,
            communities_per_level={0: 2, 1: 3},
            entities_processed=12,
        ),
    )
    resp = await client.post(f"/api/v1/graphs/{_GRAPH}/communities/detect", headers=_AUTH, json={})
    assert resp.status_code == 200  # inline detect → 200 (the status code carries sync vs async)
    body = resp.json()
    assert body["status"] == "completed"
    assert body["total_communities"] == 5


async def test_detect_async_returns_job(client, svc) -> None:
    job = IngestionJobRecord(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        graph_id=_GRAPH,
        source_type="community_detect",
        filename=None,
        status="pending",
        progress=0,
        error_message=None,
        extracted_entities=0,
        extracted_relationships=0,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    svc.detect_returns = (job, None)
    resp = await client.post(f"/api/v1/graphs/{_GRAPH}/communities/detect", headers=_AUTH, json={})
    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] == str(job.id)
    assert body["status"] == "pending"


async def test_detect_gds_unavailable_503(client, svc) -> None:
    svc.raise_with = GdsUnavailableError("gds.* not loaded")
    resp = await client.post(f"/api/v1/graphs/{_GRAPH}/communities/detect", headers=_AUTH, json={})
    assert resp.status_code == 503


async def test_detect_graph_not_found_404(client, svc) -> None:
    svc.raise_with = GraphNotFound(str(_GRAPH))
    resp = await client.post(f"/api/v1/graphs/{_GRAPH}/communities/detect", headers=_AUTH, json={})
    assert resp.status_code == 404


async def test_list_communities_shape(client, svc) -> None:
    svc.communities = [_community()]
    resp = await client.get(f"/api/v1/graphs/{_GRAPH}/communities", headers=_AUTH)
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["community_id"] == "community_abc"
    assert item["size"] == 3
    assert item["label"]  # derived from the summary
    assert item["summary_keywords"] == ["Alice", "Bob"]
    assert item["summary_source"] == "llm"  # provenance surfaced on the wire


async def test_list_unknown_kind_400(client, svc) -> None:
    svc.raise_with = UnknownCommunityKind("bogus")
    resp = await client.get(f"/api/v1/graphs/{_GRAPH}/communities?kind=bogus", headers=_AUTH)
    assert resp.status_code == 400


async def test_get_community_404_when_absent(client, svc) -> None:
    svc.community = None
    resp = await client.get(f"/api/v1/graphs/{_GRAPH}/communities/community_x", headers=_AUTH)
    assert resp.status_code == 404


async def test_get_community_200(client, svc) -> None:
    svc.community = _community("community_x")
    resp = await client.get(f"/api/v1/graphs/{_GRAPH}/communities/community_x", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["community_id"] == "community_x"
    assert resp.json()["members"][0]["entity_name"] == "Alice"


async def test_status_endpoint(client) -> None:
    resp = await client.get(f"/api/v1/graphs/{_GRAPH}/communities/status", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    assert resp.json()["communities_count"] == 3


async def test_summarize_unavailable_503(client, svc) -> None:
    svc.raise_with = SummarizationUnavailable("not configured")
    resp = await client.post(f"/api/v1/graphs/{_GRAPH}/communities/summarize", headers=_AUTH)
    assert resp.status_code == 503


async def test_summarize_ok(client, svc) -> None:
    svc.summarized = 4
    resp = await client.post(f"/api/v1/graphs/{_GRAPH}/communities/summarize", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["summarized"] == 4


async def test_analytics_endpoint(client) -> None:
    resp = await client.get(f"/api/v1/graphs/{_GRAPH}/analytics", headers=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_count"] == 10
    assert body["entity_types"][0]["label"] == "Person"
    assert body["community_count"] == 3
