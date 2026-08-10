"""Every pure read composes the ORG-scoped gate; every write keeps the OWNER gate (#736, ADR-051).

`GraphService.get_graph` has twelve call sites across four services, and they mix reads with writes.
Relaxing it in place would widen ingest, the ontology write and resolution decisions — a direct
violation of ADR-051 decision 3 that a test covering only the two routes the ADR names would never
catch. So each call site is pinned here by behaviour, over a REAL `GraphService` backed by a fake
repository, not by a spy on the gate:

* a same-org member who owns nothing gets through every read;
* the same member is refused on every write;
* a caller in another organisation is refused on both.

The boundary ruled on #736 is "every pure read in this service", so graph detail, artifacts, the
ingest-job status, the ontology read and the analytics reads are all included — not only the two
routes ADR-051 names by example.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound, GraphService
from oraclous_knowledge_graph_service.services.job_service import JobService
from oraclous_knowledge_graph_service.services.ontology_service import OntologyService

# api_authz: with test_org_scoped_read_gate.py this file carries every per-call-site cross-org
# negative for the widened read gate, so the T1 coverage gate must collect it.
pytestmark = [pytest.mark.unit, pytest.mark.api_authz]

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005ff")
_OWNER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_MEMBER = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_NOW = datetime(2026, 8, 8, tzinfo=UTC)


def _graph(*, user_id: uuid.UUID, organisation_id: uuid.UUID = _ORG) -> Graph:
    return Graph(
        id=uuid.uuid4(),
        organisation_id=organisation_id,
        user_id=user_id,
        name="Company Knowledge Base",
        description=None,
        status="active",
        node_count=0,
        relationship_count=0,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _GraphRepo:
    """Org-scoped like the real repository: a row outside `bound_org` is invisible."""

    def __init__(self, bound_org: uuid.UUID = _ORG) -> None:
        self.bound_org = bound_org
        self.rows: dict[uuid.UUID, Graph] = {}

    def seed(self, graph: Graph) -> Graph:
        self.rows[graph.id] = graph
        return graph

    async def list_for_org(self) -> list[Graph]:
        return [g for g in self.rows.values() if g.organisation_id == self.bound_org]

    async def get(self, graph_id) -> Graph | None:
        g = self.rows.get(graph_id)
        return g if g is not None and g.organisation_id == self.bound_org else None

    async def get_ontology(self, graph_id) -> dict | None:
        return {"allowed_labels": ["Person"], "mode": "strict"}


def _job(graph_id: uuid.UUID, *, filename: str = "canon.md") -> IngestionJobRecord:
    return IngestionJobRecord(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        graph_id=graph_id,
        source_type="text",
        filename=filename,
        status="completed",
        progress=100,
        error_message=None,
        extracted_entities=1,
        extracted_relationships=0,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _JobRepo:
    def __init__(self) -> None:
        self.rows: list[IngestionJobRecord] = []
        self.created: list[dict] = []

    def seed(self, rec: IngestionJobRecord) -> IngestionJobRecord:
        self.rows.append(rec)
        return rec

    async def list_for_graph(self, graph_id, *, exclude_source_types=(), **_filters):
        return [r for r in self.rows if r.graph_id == graph_id]

    async def get(self, job_id):
        return next((r for r in self.rows if r.id == job_id), None)

    async def get_source_content(self, job_id):
        return None

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return _job(kwargs["graph_id"])

    async def commit(self) -> None:
        return None


@pytest.fixture
def graph_repo() -> _GraphRepo:
    return _GraphRepo()


@pytest.fixture
def graphs(graph_repo: _GraphRepo) -> GraphService:
    return GraphService(graph_repo)


@pytest.fixture
def owners_graph(graph_repo: _GraphRepo) -> Graph:
    return graph_repo.seed(_graph(user_id=_OWNER))


@pytest.fixture
def job_repo() -> _JobRepo:
    return _JobRepo()


@pytest.fixture
def jobs(job_repo: _JobRepo, graphs: GraphService) -> JobService:
    return JobService(job_repo=job_repo, graph_service=graphs, enqueue=lambda *_a, **_k: None)


def _org_context(user_id: uuid.UUID = _OWNER):
    """`submit` reads the bound org to hand it across the Celery boundary, so the ingest tests bind
    one. It is the caller's own org — the gate above it is what decides access."""
    return use_organisation_context(
        OrganisationContext(
            organisation_id=_ORG, principal_id=user_id, principal_type=PrincipalType.USER
        )
    )


def _outsider(graph_repo: _GraphRepo) -> _GraphRepo:
    """The same stored graphs seen from another organisation's bound context."""
    other = _GraphRepo(bound_org=_OTHER_ORG)
    other.rows = graph_repo.rows
    return other


# ── documents: the read ADR-051 names ─────────────────────────────────────────────────────────


async def test_a_member_lists_documents_of_a_graph_they_do_not_own(
    jobs, job_repo, owners_graph
) -> None:
    """Acceptance 2. Today this is a 404 — the member can search the graph but not see what is in
    it."""
    job_repo.seed(_job(owners_graph.id))
    docs = await jobs.list_documents(user_id=_MEMBER, graph_id=owners_graph.id)
    assert [d.filename for d in docs] == ["canon.md"]


async def test_another_organisation_still_cannot_list_documents(
    job_repo, graph_repo, owners_graph
) -> None:
    job_repo.seed(_job(owners_graph.id))
    outsider_jobs = JobService(
        job_repo=job_repo,
        graph_service=GraphService(_outsider(graph_repo)),
        enqueue=lambda *_a, **_k: None,
    )
    with pytest.raises(GraphNotFound):
        await outsider_jobs.list_documents(user_id=_MEMBER, graph_id=owners_graph.id)


# ── artifacts: reached through the same gate, so make it deliberate ───────────────────────────


async def test_a_member_lists_artifacts_of_a_graph_they_do_not_own(
    jobs, job_repo, owners_graph
) -> None:
    job_repo.seed(_job(owners_graph.id))
    found = await jobs.list_artifacts(user_id=_MEMBER, graph_id=owners_graph.id)
    assert [a.filename for a in found] == ["canon.md"]


async def test_a_member_reads_one_artifacts_content(jobs, job_repo, owners_graph) -> None:
    """`GET /v1/artifacts/{id}` serves verbatim document content. It is the same content the member
    can already retrieve through the org-scoped `POST /v1/search/*`, through a different door."""
    rec = job_repo.seed(_job(owners_graph.id))
    got, _content = await jobs.get_artifact(user_id=_MEMBER, artifact_id=rec.id)
    assert got.id == rec.id


async def test_another_organisation_still_cannot_read_an_artifact(
    job_repo, graph_repo, owners_graph
) -> None:
    rec = job_repo.seed(_job(owners_graph.id))
    outsider_jobs = JobService(
        job_repo=job_repo,
        graph_service=GraphService(_outsider(graph_repo)),
        enqueue=lambda *_a, **_k: None,
    )
    with pytest.raises(GraphNotFound):
        await outsider_jobs.get_artifact(user_id=_MEMBER, artifact_id=rec.id)


async def test_a_member_polls_an_ingest_jobs_status(jobs, job_repo, owners_graph) -> None:
    rec = job_repo.seed(_job(owners_graph.id))
    got = await jobs.get_job(user_id=_MEMBER, graph_id=owners_graph.id, job_id=rec.id)
    assert got.id == rec.id


# ── ingest: the write ADR-051 decision 3 protects ─────────────────────────────────────────────


async def test_a_member_cannot_ingest_into_a_graph_they_can_read(jobs, job_repo, owners_graph):
    """Acceptance 3, and the trap this whole shape exists to avoid. The member reads the graph one
    line above and is still refused the write."""
    assert await jobs.list_documents(user_id=_MEMBER, graph_id=owners_graph.id) == []
    with _org_context(_MEMBER), pytest.raises(GraphNotFound):
        await jobs.submit(
            user_id=_MEMBER,
            graph_id=owners_graph.id,
            source_type="text",
            filename="smuggled.md",
            data=b"payload",
        )
    assert job_repo.created == []  # no row was minted before the gate ran


async def test_the_owner_can_still_ingest(jobs, job_repo, owners_graph) -> None:
    """The owner gate did not become a wall: the widening cost the owner nothing."""
    with _org_context(_OWNER):
        await jobs.submit(
            user_id=_OWNER,
            graph_id=owners_graph.id,
            source_type="text",
            filename="canon.md",
            data=b"payload",
        )
    assert len(job_repo.created) == 1


# ── ontology: read widens, write does not ─────────────────────────────────────────────────────


async def test_a_member_reads_the_ontology(graph_repo, graphs, owners_graph) -> None:
    svc = OntologyService(graph_repo, graphs)  # type: ignore[arg-type]
    assert (await svc.get(user_id=_MEMBER, graph_id=owners_graph.id))["mode"] == "strict"


async def test_a_member_cannot_write_the_ontology(graph_repo, graphs, owners_graph) -> None:
    svc = OntologyService(graph_repo, graphs)  # type: ignore[arg-type]
    with pytest.raises(GraphNotFound):
        await svc.set(
            user_id=_MEMBER, graph_id=owners_graph.id, allowed_labels=["Person"], mode="strict"
        )


async def test_another_organisation_still_cannot_read_the_ontology(graph_repo, owners_graph):
    svc = OntologyService(graph_repo, GraphService(_outsider(graph_repo)))  # type: ignore[arg-type]
    with pytest.raises(GraphNotFound):
        await svc.get(user_id=_MEMBER, graph_id=owners_graph.id)


# ── analytics: the read half widens, detect and summarise do not ──────────────────────────────


class _CommunityRepo:
    def list_communities(self, *, graph_id, level=None, min_entities=1):
        return []

    def status(self, *, graph_id):
        return (0, [], 7)

    def analytics(self, *, graph_id):
        return {
            "node_count": 7,
            "relationship_count": 3,
            "entity_count": 7,
            "density": 0.1,
            "avg_degree": 0.9,
            "entity_types": [],
            "relationship_types": [],
            "top_entities": [],
            "community_count": 0,
        }


async def test_a_member_reads_graph_analytics(graphs, owners_graph) -> None:
    from oraclous_knowledge_graph_service.services.analytics_service import AnalyticsService

    svc = AnalyticsService(graph_service=graphs, repo=_CommunityRepo())  # type: ignore[arg-type]
    stats = await svc.analytics(user_id=_MEMBER, graph_id=owners_graph.id)
    assert stats.node_count == 7
    assert (await svc.status(user_id=_MEMBER, graph_id=owners_graph.id)).entity_count == 7
    assert await svc.list_communities(user_id=_MEMBER, graph_id=owners_graph.id) == []


async def test_a_member_cannot_run_community_detection(graphs, owners_graph) -> None:
    """`detect` writes communities into the substrate, so it stays on the owner gate."""
    from oraclous_knowledge_graph_service.services.analytics_service import AnalyticsService

    svc = AnalyticsService(graph_service=graphs, repo=_CommunityRepo())  # type: ignore[arg-type]
    with pytest.raises(GraphNotFound):
        await svc.detect(user_id=_MEMBER, graph_id=owners_graph.id)


async def test_another_organisation_still_cannot_read_analytics(graph_repo, owners_graph) -> None:
    from oraclous_knowledge_graph_service.services.analytics_service import AnalyticsService

    svc = AnalyticsService(
        graph_service=GraphService(_outsider(graph_repo)),
        repo=_CommunityRepo(),  # type: ignore[arg-type]
    )
    with pytest.raises(GraphNotFound):
        await svc.analytics(user_id=_MEMBER, graph_id=owners_graph.id)
