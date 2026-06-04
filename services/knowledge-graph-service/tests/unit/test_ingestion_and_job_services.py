"""Unit tests for IngestionService + JobService (fakes; no Neo4j/Postgres/broker)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord
from oraclous_knowledge_graph_service.repositories.graph_write_repository import WriteResult
from oraclous_knowledge_graph_service.services.embedder import HashingEmbedder
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound
from oraclous_knowledge_graph_service.services.ingestion_service import (
    IngestionError,
    IngestionService,
)
from oraclous_knowledge_graph_service.services.job_service import JobNotFound, JobService

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_USER = uuid.uuid4()
_GRAPH = uuid.uuid4()


# --- IngestionService ---------------------------------------------------------
class _FakeWriteRepo:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def write_document(self, *, graph_id, document, chunks, embeddings, title=None):
        self.calls.append({"graph_id": graph_id, "document": document, "chunks": chunks})
        return WriteResult(nodes=1 + len(chunks), relationships=len(chunks), chunks=len(chunks))


async def test_ingest_text_chunks_and_writes() -> None:
    repo = _FakeWriteRepo()
    svc = IngestionService(repo, HashingEmbedder(dim=8))
    result = await svc.ingest(
        graph_id="g1", document="d.txt", data=b"para one\n\npara two", source_type="text"
    )
    assert result.chunks == 2
    assert result.nodes == 3
    assert repo.calls[0]["chunks"] == ["para one", "para two"]


async def test_ingest_empty_text_raises() -> None:
    svc = IngestionService(_FakeWriteRepo(), HashingEmbedder(dim=8))
    with pytest.raises(IngestionError):
        await svc.ingest(graph_id="g1", document="d.txt", data=b"   ", source_type="text")


# --- JobService ---------------------------------------------------------------
def _record(graph_id: uuid.UUID, status: str = "pending") -> IngestionJobRecord:
    now = datetime(2026, 6, 4, tzinfo=UTC)
    return IngestionJobRecord(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        graph_id=graph_id,
        source_type="text",
        filename="a.txt",
        status=status,
        progress=0,
        error_message=None,
        extracted_entities=0,
        extracted_relationships=0,
        created_at=now,
        updated_at=now,
    )


class _FakeJobRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, IngestionJobRecord] = {}

    async def create(self, *, graph_id, source_type, filename, source_content, recipe_id=None, **_):
        rec = _record(graph_id)
        self.rows[rec.id] = rec
        return rec

    async def get(self, job_id):
        return self.rows.get(job_id)

    async def list_for_graph(self, graph_id):
        return [r for r in self.rows.values() if r.graph_id == graph_id]


class _FakeGraphService:
    def __init__(self, owned: set[uuid.UUID]) -> None:
        self._owned = owned

    async def get_graph(self, *, graph_id, user_id):
        if graph_id not in self._owned:
            raise GraphNotFound(str(graph_id))
        now = datetime(2026, 6, 4, tzinfo=UTC)
        return Graph(
            id=graph_id,
            organisation_id=_ORG,
            user_id=user_id,
            name="g",
            description=None,
            status="active",
            node_count=0,
            relationship_count=0,
            created_at=now,
            updated_at=now,
        )


def _service(owned: set[uuid.UUID], enqueued: list[tuple[str, str]]) -> JobService:
    return JobService(
        job_repo=_FakeJobRepo(),
        graph_service=_FakeGraphService(owned),
        enqueue=lambda j, o: enqueued.append((j, o)),
    )


def _ctx():
    return use_organisation_context(
        OrganisationContext(
            organisation_id=_ORG, principal_id=_USER, principal_type=PrincipalType.USER
        )
    )


async def test_submit_creates_and_enqueues_with_org() -> None:
    enqueued: list[tuple[str, str]] = []
    svc = _service({_GRAPH}, enqueued)
    with _ctx():
        job = await svc.submit(
            user_id=_USER, graph_id=_GRAPH, data=b"hi", filename="a.txt", source_type="text"
        )
    assert enqueued and enqueued[0][0] == str(job.id)
    assert enqueued[0][1] == str(_ORG)  # org passed across the broker boundary


async def test_submit_unowned_graph_raises_not_found() -> None:
    enqueued: list[tuple[str, str]] = []
    svc = _service(set(), enqueued)
    with _ctx(), pytest.raises(GraphNotFound):
        await svc.submit(
            user_id=_USER, graph_id=_GRAPH, data=b"hi", filename="a.txt", source_type="text"
        )
    assert enqueued == []  # nothing enqueued on a denied submit


async def test_get_job_unowned_graph_raises() -> None:
    svc = _service(set(), [])
    with _ctx(), pytest.raises(GraphNotFound):
        await svc.get_job(user_id=_USER, graph_id=_GRAPH, job_id=uuid.uuid4())


async def test_get_missing_job_raises_job_not_found() -> None:
    svc = _service({_GRAPH}, [])
    with _ctx(), pytest.raises(JobNotFound):
        await svc.get_job(user_id=_USER, graph_id=_GRAPH, job_id=uuid.uuid4())
