"""Batch/folder content ingest HTTP layer (#522, E6 — the cloud content-in flow).

The cloud product needs a user to land a FOLDER/REPO of content in their org graph in one call (not
file-by-file): the book author's git-markdown (bible/rules/drafts), DoefinGPT's project docs, the
EURail evidence corpus. This is thin orchestration over the existing single-ingest seam — one async
ingest job enqueued per item, each idempotent on its path (re-ingest replaces, never duplicates).

Real route + dev-auth + an in-memory JobService (no Postgres/Neo4j/broker — the live landing → Neo4j
is the deployed-stack e2e). RED until #522 [impl] adds POST /api/v1/graphs/{id}/batch-ingest.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.core.dependencies import get_job_service
from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}
_ORG = uuid.UUID("00000000-0000-0000-0000-00000000052a")


def _record(graph_id: uuid.UUID, filename: str, source_type: str) -> IngestionJobRecord:
    now = datetime(2026, 6, 24, tzinfo=UTC)
    return IngestionJobRecord(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        graph_id=graph_id,
        source_type=source_type,
        filename=filename,
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
        self.submitted: list[dict] = []
        self.owned = True

    async def submit(self, *, user_id, graph_id, data, filename, source_type, recipe_id=None, **_):
        if not self.owned:
            raise GraphNotFound(str(graph_id))
        self.submitted.append({"filename": filename, "source_type": source_type, "data": data})
        return _record(graph_id, filename, source_type)


@pytest.fixture
def fake_service() -> _FakeJobService:
    return _FakeJobService()


@pytest.fixture
def client(app, async_client, fake_service):
    app.dependency_overrides[get_job_service] = lambda: fake_service
    yield async_client
    app.dependency_overrides.clear()


def _items() -> list[dict]:
    return [
        {
            "path": "bible/canon.md",
            "content": "# Canon\nThe world is round.",
            "source_type": "text",
        },
        {"path": "rules/style.md", "content": "# Style\nUse the active voice."},
        {"path": "drafts/ch1.md", "content": "# Chapter 1\nIt was a dark night."},
    ]


async def test_batch_ingest_requires_auth(client) -> None:
    resp = await client.post(
        f"/api/v1/graphs/{uuid.uuid4()}/batch-ingest", json={"items": _items()}
    )
    assert resp.status_code == 401


async def test_batch_ingest_enqueues_one_job_per_item(client, fake_service) -> None:
    """A folder of N items → one async ingest job per item; org-scoped (org never a body field)."""
    gid = uuid.uuid4()
    resp = await client.post(
        f"/api/v1/graphs/{gid}/batch-ingest", json={"items": _items()}, headers=_AUTH
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "organisation_id" not in body
    assert len(body["jobs"]) == 3
    assert all(j["status"] == "pending" and j["id"] for j in body["jobs"])
    # each item was submitted with its path as the document identity (→ idempotent per path)
    assert {s["filename"] for s in fake_service.submitted} == {
        "bible/canon.md",
        "rules/style.md",
        "drafts/ch1.md",
    }


async def test_batch_ingest_rejects_an_empty_batch(client) -> None:
    resp = await client.post(
        f"/api/v1/graphs/{uuid.uuid4()}/batch-ingest", json={"items": []}, headers=_AUTH
    )
    assert resp.status_code == 422, resp.text
