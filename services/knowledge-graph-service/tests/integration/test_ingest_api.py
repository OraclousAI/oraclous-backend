"""Ingestion HTTP layer (R3.5-P1-S2) — real routes + dev-auth + an in-memory JobService.

No Postgres/Neo4j/broker: `get_job_service` is overridden with a fake. The auth seam (401), the
upload type-validation (422), the empty-content guard (422), and the owner-gate mapping (404) are
all real route behaviour. Live ingestion → Neo4j is covered by the docker smoke.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.core.dependencies import get_job_service
from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound
from oraclous_knowledge_graph_service.services.job_service import JobNotFound

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}
_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")


def _record(graph_id: uuid.UUID) -> IngestionJobRecord:
    now = datetime(2026, 6, 4, tzinfo=UTC)
    return IngestionJobRecord(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        graph_id=graph_id,
        source_type="text",
        filename="a.txt",
        status="pending",
        progress=0,
        error_message=None,
        extracted_entities=0,
        extracted_relationships=0,
        created_at=now,
        updated_at=now,
    )


class _FakeJobService:
    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, IngestionJobRecord] = {}
        self.owned = True

    async def submit(self, *, user_id, graph_id, data, filename, source_type, recipe_id=None):
        if not self.owned:
            raise GraphNotFound(str(graph_id))
        rec = _record(graph_id)
        self.jobs[rec.id] = rec
        return rec

    async def get_job(self, *, user_id, graph_id, job_id):
        if not self.owned:
            raise GraphNotFound(str(graph_id))
        if job_id not in self.jobs:
            raise JobNotFound(str(job_id))
        return self.jobs[job_id]

    async def list_documents(self, *, user_id, graph_id):
        if not self.owned:
            raise GraphNotFound(str(graph_id))
        return list(self.jobs.values())


@pytest.fixture
def fake_service() -> _FakeJobService:
    return _FakeJobService()


@pytest.fixture
def client(app, async_client, fake_service):
    app.dependency_overrides[get_job_service] = lambda: fake_service
    yield async_client
    app.dependency_overrides.clear()


async def test_ingest_requires_auth(client) -> None:
    resp = await client.post(f"/api/v1/graphs/{uuid.uuid4()}/ingest", json={"content": "hi"})
    assert resp.status_code == 401


async def test_ingest_text_returns_202(client) -> None:
    gid = uuid.uuid4()
    resp = await client.post(
        f"/api/v1/graphs/{gid}/ingest", json={"content": "hello world"}, headers=_AUTH
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["source_type"] == "text"
    assert "organisation_id" not in body


async def test_ingest_empty_content_is_422(client) -> None:
    resp = await client.post(
        f"/api/v1/graphs/{uuid.uuid4()}/ingest", json={"content": ""}, headers=_AUTH
    )
    assert resp.status_code == 422


async def test_ingest_unowned_graph_is_404(client, fake_service) -> None:
    fake_service.owned = False
    resp = await client.post(
        f"/api/v1/graphs/{uuid.uuid4()}/ingest", json={"content": "hi"}, headers=_AUTH
    )
    assert resp.status_code == 404


async def test_get_and_list_documents(client) -> None:
    gid = uuid.uuid4()
    created = (
        await client.post(f"/api/v1/graphs/{gid}/ingest", json={"content": "hi"}, headers=_AUTH)
    ).json()
    got = await client.get(f"/api/v1/graphs/{gid}/jobs/{created['id']}", headers=_AUTH)
    assert got.status_code == 200
    docs = await client.get(f"/api/v1/graphs/{gid}/documents", headers=_AUTH)
    assert docs.status_code == 200 and len(docs.json()) == 1


async def test_get_missing_job_is_404(client) -> None:
    resp = await client.get(f"/api/v1/graphs/{uuid.uuid4()}/jobs/{uuid.uuid4()}", headers=_AUTH)
    assert resp.status_code == 404


async def test_upload_text_returns_202(client) -> None:
    files = {"file": ("a.txt", b"hello\n\nworld", "text/plain")}
    resp = await client.post(f"/api/v1/graphs/{uuid.uuid4()}/upload", files=files, headers=_AUTH)
    assert resp.status_code == 202, resp.text


async def test_upload_unsupported_type_is_422(client) -> None:
    files = {"file": ("a.exe", b"x", "application/octet-stream")}
    resp = await client.post(f"/api/v1/graphs/{uuid.uuid4()}/upload", files=files, headers=_AUTH)
    assert resp.status_code == 422
