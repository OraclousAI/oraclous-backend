"""Unit tests for AnalyticsService (#303) with fakes — no Neo4j, no LLM.

Covers the orchestration contract: detect skips below the entity floor, above the entity cap, and
when communities already exist without force_rebuild; the sync/async routing decision in
submit_detect (the boundary, the enqueue, the bound org id); list/get/status/analytics delegate
correctly; the owner gate maps a cross-org/non-owned graph to GraphNotFound (→404); an unknown kind
is rejected; status folds in a running/failed detect job; summarise is unavailable when no
summarizer is configured.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import use_organisation_context
from oraclous_knowledge_graph_service.domain.community import (
    COMMUNITY_DETECT_SOURCE_TYPE,
    Community,
    CommunityMember,
    DetectionInProgress,
)
from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord
from oraclous_knowledge_graph_service.services.analytics_service import (
    AnalyticsService,
    SummarizationUnavailable,
    UnknownCommunityKind,
    decode_detect_params,
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
        self,
        *,
        entity_count: int = 0,
        communities: list[Community] | None = None,
        detect_levels: dict | None = None,
        raise_in_progress: bool = False,
    ) -> None:
        self._entity_count = entity_count
        self._communities = communities or []
        # Default: an honest 2-level dendrogram result (NOT five duplicates).
        self._detect_levels = (
            detect_levels
            if detect_levels is not None
            else {0: {"L0:1": ["e1", "e2", "e3"]}, 1: {"L1:1": ["e1", "e2"], "L1:3": ["e3"]}}
        )
        self._raise_in_progress = raise_in_progress
        self.detect_called = False

    def count_entities(self, *, graph_id: str) -> int:  # noqa: ARG002
        return self._entity_count

    def detect(self, *, graph_id: str):  # noqa: ANN001, ARG002
        self.detect_called = True
        if self._raise_in_progress:
            raise DetectionInProgress(graph_id)
        return self._detect_levels

    def list_communities(self, *, graph_id, level, min_entities, only_unsummarized=False):  # noqa: ANN001, ARG002
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


class _FakeJobRepo:
    """Records create/commit/enqueue and serves the latest detect job for the status fold-in."""

    def __init__(self, *, latest_detect: IngestionJobRecord | None = None) -> None:
        self.created: list[dict] = []
        self.committed = False
        self._latest_detect = latest_detect

    async def create(self, *, graph_id, source_type, filename, source_content):  # noqa: ANN001
        self.created.append(
            {
                "graph_id": graph_id,
                "source_type": source_type,
                "source_content": source_content,
            }
        )
        return IngestionJobRecord(
            id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
            graph_id=graph_id,
            source_type=source_type,
            filename=filename,
            status="pending",
            progress=0,
            error_message=None,
            extracted_entities=0,
            extracted_relationships=0,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def commit(self) -> None:
        self.committed = True

    async def latest_by_source_type(self, graph_id, *, source_type):  # noqa: ANN001, ARG002
        return self._latest_detect


def _svc(
    repo: _FakeRepo, *, owned: bool = True, summarizer=None, job_repo=None, enqueue=None
) -> AnalyticsService:
    return AnalyticsService(
        graph_service=_FakeGraphService(owned={_GRAPH} if owned else set()),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        job_repo=job_repo,
        enqueue=enqueue,
        summarizer=summarizer,
    )


def _ctx(org: uuid.UUID) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=org, principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


def _community(level: int = 0, cid: str = "community_abc") -> Community:
    return Community(
        community_id=cid,
        kind="entity",
        level=level,
        entity_count=3,
        status="active",
        members=[CommunityMember(entity_id="e1", entity_name="Alice", entity_type="Person")],
    )


async def test_detect_skips_below_floor() -> None:
    svc = _svc(_FakeRepo(entity_count=1))
    result = await svc.detect(graph_id=_GRAPH, user_id=_USER, min_entities=3)
    assert result.status == "skipped"
    assert result.total_communities == 0


async def test_detect_emits_honest_dendrogram_levels() -> None:
    # The native dendrogram result (2 honest levels), NOT five duplicates from a resolution sweep.
    repo = _FakeRepo(entity_count=50)
    svc = _svc(repo)
    result = await svc.detect(graph_id=_GRAPH, user_id=_USER)
    assert result.status == "completed"
    assert repo.detect_called
    assert set(result.communities_per_level) == {0, 1}
    assert result.communities_per_level == {0: 1, 1: 2}  # coarsest 1, finer 2


async def test_detect_skips_above_entity_cap(monkeypatch) -> None:
    from oraclous_knowledge_graph_service.core import config

    monkeypatch.setenv("KGS_COMMUNITY_MAX_ENTITIES", "10")
    config.get_settings.cache_clear()
    try:
        repo = _FakeRepo(entity_count=50)
        result = await _svc(repo).detect(graph_id=_GRAPH, user_id=_USER, force_rebuild=True)
        assert result.status == "skipped"
        assert "exceeds maximum" in (result.reason or "")
        assert repo.detect_called is False
    finally:
        config.get_settings.cache_clear()


async def test_detect_skips_when_communities_exist_without_force() -> None:
    # Communities already detected and force_rebuild False → no-op skip (no destructive rebuild).
    repo = _FakeRepo(entity_count=50, communities=[_community(level=0)])
    svc = _svc(repo)
    result = await svc.detect(graph_id=_GRAPH, user_id=_USER, force_rebuild=False)
    assert result.status == "skipped"
    assert repo.detect_called is False
    # force_rebuild True overrides and runs.
    repo2 = _FakeRepo(entity_count=50, communities=[_community(level=0)])
    result2 = await _svc(repo2).detect(graph_id=_GRAPH, user_id=_USER, force_rebuild=True)
    assert result2.status == "completed"
    assert repo2.detect_called is True


async def test_detect_already_in_progress_is_skip() -> None:
    repo = _FakeRepo(entity_count=50, raise_in_progress=True)
    result = await _svc(repo).detect(graph_id=_GRAPH, user_id=_USER, force_rebuild=True)
    assert result.status == "skipped"
    assert "in progress" in (result.reason or "")


async def test_owner_gate_blocks_cross_org_detect() -> None:
    # A graph the caller does not own (e.g. another org's) → GraphNotFound (→404, no leak).
    svc = _svc(_FakeRepo(entity_count=50), owned=False)
    with pytest.raises(GraphNotFound):
        await svc.detect(graph_id=_GRAPH, user_id=_USER)


_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


async def test_submit_detect_routes_small_graph_sync_no_enqueue() -> None:
    # <= threshold → inline; no job created, no enqueue.
    repo = _FakeRepo(entity_count=5)
    jobs = _FakeJobRepo()
    enqueued: list[tuple[str, str]] = []
    svc = _svc(repo, job_repo=jobs, enqueue=lambda jid, org: enqueued.append((jid, org)))
    with use_organisation_context(_ctx(_ORG)):
        job, result = await svc.submit_detect(graph_id=_GRAPH, user_id=_USER, force_rebuild=True)
    assert job is None and result is not None and result.status == "completed"
    assert jobs.created == []
    assert enqueued == []


async def test_submit_detect_routes_large_graph_async_with_bound_org() -> None:
    # > threshold → a committed job + an enqueue carrying the BOUND org id and the request params.
    repo = _FakeRepo(entity_count=10_000)
    jobs = _FakeJobRepo()
    enqueued: list[tuple[str, str]] = []
    svc = _svc(repo, job_repo=jobs, enqueue=lambda jid, org: enqueued.append((jid, org)))
    with use_organisation_context(_ctx(_ORG)):
        job, result = await svc.submit_detect(
            graph_id=_GRAPH, user_id=_USER, min_entities=7, force_rebuild=True
        )
    assert result is None and job is not None
    assert jobs.committed is True  # committed BEFORE enqueue (read-after-write, #267)
    assert repo.detect_called is False  # the worker runs it, not the request
    assert len(enqueued) == 1
    assert enqueued[0] == (str(job.id), str(_ORG))  # the bound org id, not a caller arg
    # The request params ride on source_content so the worker applies them.
    me, force = decode_detect_params(jobs.created[0]["source_content"])
    assert me == 7
    assert force is True


async def test_submit_detect_boundary_runs_sync_at_threshold() -> None:
    # Exactly AT the threshold (300) → inline (the boundary is inclusive of sync).
    repo = _FakeRepo(entity_count=300)
    jobs = _FakeJobRepo()
    enqueued: list[tuple[str, str]] = []
    svc = _svc(repo, job_repo=jobs, enqueue=lambda jid, org: enqueued.append((jid, org)))
    with use_organisation_context(_ctx(_ORG)):
        job, result = await svc.submit_detect(graph_id=_GRAPH, user_id=_USER, force_rebuild=True)
    assert job is None and result is not None
    assert enqueued == []


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


def _detect_job(state: str) -> IngestionJobRecord:
    return IngestionJobRecord(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        graph_id=_GRAPH,
        source_type=COMMUNITY_DETECT_SOURCE_TYPE,
        filename=None,
        status=state,
        progress=0,
        error_message=None,
        extracted_entities=0,
        extracted_relationships=0,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


async def test_status_folds_in_running_detect_job() -> None:
    # No communities in the substrate yet (mid-run, right after the clear) BUT a running detect job
    # → status 'running', not the split-brain 'not_detected'.
    repo = _FakeRepo(entity_count=5)
    jobs = _FakeJobRepo(latest_detect=_detect_job("running"))
    status = await _svc(repo, job_repo=jobs).status(graph_id=_GRAPH, user_id=_USER)
    assert status.status == "running"


async def test_status_folds_in_failed_detect_job() -> None:
    repo = _FakeRepo(entity_count=5)
    jobs = _FakeJobRepo(latest_detect=_detect_job("failed"))
    status = await _svc(repo, job_repo=jobs).status(graph_id=_GRAPH, user_id=_USER)
    assert status.status == "failed"


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
