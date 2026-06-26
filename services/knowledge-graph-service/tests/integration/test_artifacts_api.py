"""/v1/artifacts — the unified artifact read/serve surface (#543, ADR-041).

A team's outputs live on Oraclous (graph-indexed) and are SERVED here: list the artifacts (the
ingested documents) for a graph — optionally filtered by a filename/content query or source_type —
and fetch one artifact's verbatim content. Org-scoped (the org is bound from the principal via graph
ownership, never a body field; ``organisation_id`` is never exposed). Route-level behaviour over a
fake JobService (the real DB path is the docker smoke / the #543 e2e).
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


def _record(graph_id: uuid.UUID, *, filename: str, source_type: str = "text") -> IngestionJobRecord:
    now = datetime(2026, 6, 25, tzinfo=UTC)
    return IngestionJobRecord(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        graph_id=graph_id,
        source_type=source_type,
        filename=filename,
        status="completed",
        progress=100,
        error_message=None,
        extracted_entities=3,
        extracted_relationships=2,
        created_at=now,
        updated_at=now,
    )


class _FakeJobService:
    def __init__(self) -> None:
        self.owned = True
        self.records: dict[uuid.UUID, tuple[IngestionJobRecord, str | None]] = {}

    def add(self, rec: IngestionJobRecord, content: str | None) -> None:
        self.records[rec.id] = (rec, content)

    async def list_artifacts(self, *, user_id, graph_id, q=None, source_type=None):
        if not self.owned:
            raise GraphNotFound(str(graph_id))
        out = [r for (r, _c) in self.records.values() if r.graph_id == graph_id]
        if source_type:
            out = [r for r in out if r.source_type == source_type]
        if q:
            out = [r for r in out if q.lower() in (r.filename or "").lower()]
        return out

    async def get_artifact(self, *, user_id, artifact_id):
        if not self.owned:
            raise GraphNotFound("x")
        if artifact_id not in self.records:
            raise JobNotFound(str(artifact_id))
        return self.records[artifact_id]


@pytest.fixture
def fake_service() -> _FakeJobService:
    return _FakeJobService()


@pytest.fixture
def client(app, async_client, fake_service):
    app.dependency_overrides[get_job_service] = lambda: fake_service
    yield async_client
    app.dependency_overrides.clear()


async def test_list_artifacts_requires_auth(client) -> None:
    resp = await client.get(f"/v1/artifacts?graph_id={uuid.uuid4()}")
    assert resp.status_code == 401


async def test_list_artifacts_for_a_graph(client, fake_service) -> None:
    gid = uuid.uuid4()
    fake_service.add(_record(gid, filename="bible/canon.md"), "V1")
    fake_service.add(_record(gid, filename="drafts/ch1.md"), "draft")
    resp = await client.get(f"/v1/artifacts?graph_id={gid}", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    assert {a["filename"] for a in body} == {"bible/canon.md", "drafts/ch1.md"}
    assert "organisation_id" not in body[0]
    assert "content" not in body[0]  # the list is summaries — no verbatim content


async def test_list_artifacts_q_filter(client, fake_service) -> None:
    gid = uuid.uuid4()
    fake_service.add(_record(gid, filename="bible/canon.md"), "V1")
    fake_service.add(_record(gid, filename="drafts/ch1.md"), "draft")
    resp = await client.get(f"/v1/artifacts?graph_id={gid}&q=canon", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1 and body[0]["filename"] == "bible/canon.md"


async def test_list_artifacts_unowned_graph_is_404(client, fake_service) -> None:
    fake_service.owned = False
    resp = await client.get(f"/v1/artifacts?graph_id={uuid.uuid4()}", headers=_AUTH)
    assert resp.status_code == 404


async def test_get_artifact_serves_verbatim_content(client, fake_service) -> None:
    gid = uuid.uuid4()
    rec = _record(gid, filename="bible/canon.md")
    fake_service.add(rec, "the verbatim artifact content")
    resp = await client.get(f"/v1/artifacts/{rec.id}", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(rec.id)
    assert body["filename"] == "bible/canon.md"
    assert body["content"] == "the verbatim artifact content"
    assert "organisation_id" not in body


async def test_get_missing_artifact_is_404(client) -> None:
    resp = await client.get(f"/v1/artifacts/{uuid.uuid4()}", headers=_AUTH)
    assert resp.status_code == 404


async def test_get_artifact_decodes_base64_stored_content() -> None:
    """Ingest stores ``source_content`` base64-encoded; the REAL service decodes it so /v1/artifacts
    serves the verbatim file (#543). The route-level fake returns the already-decoded value, so this
    exercises the decode in JobService.get_artifact directly."""
    import base64

    from oraclous_knowledge_graph_service.services.job_service import JobService

    rec = _record(uuid.uuid4(), filename="reports/thesis.md")

    class _Repo:
        async def get(self, job_id):
            return rec if job_id == rec.id else None

        async def get_source_content(self, job_id):
            return base64.b64encode(b"# verbatim\nbody").decode("ascii")

    class _Graphs:
        async def get_graph(self, *, graph_id, user_id):
            return None

    svc = JobService(
        job_repo=_Repo(),  # type: ignore[arg-type]
        graph_service=_Graphs(),  # type: ignore[arg-type]
        enqueue=lambda *_a, **_k: None,
    )
    _job, content = await svc.get_artifact(user_id=uuid.uuid4(), artifact_id=rec.id)
    assert content == "# verbatim\nbody"
