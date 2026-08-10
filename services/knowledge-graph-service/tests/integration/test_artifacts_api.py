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


def _record(
    graph_id: uuid.UUID,
    *,
    filename: str,
    source_type: str = "text",
    team_run_id: uuid.UUID | None = None,
    team_id: str | None = None,
    member_role: str | None = None,
    producer_kind: str | None = None,
) -> IngestionJobRecord:
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
        team_run_id=team_run_id,
        team_id=team_id,
        member_role=member_role,
        producer_kind=producer_kind,
    )


class _FakeJobService:
    def __init__(self) -> None:
        self.owned = True
        self.records: dict[uuid.UUID, tuple[IngestionJobRecord, str | None]] = {}
        # what the route last passed down — the provenance filters (#728) are applied in the REAL
        # service's repository query, so at this level the assertion is that they arrive intact.
        self.last_call: dict[str, object] = {}

    def add(self, rec: IngestionJobRecord, content: str | None) -> None:
        self.records[rec.id] = (rec, content)

    async def list_artifacts(
        self,
        *,
        user_id,
        graph_id,
        q=None,
        source_type=None,
        team_run_id=None,
        team_id=None,
        member_role=None,
        producer_kind=None,
    ):
        self.last_call = {
            "user_id": user_id,
            "graph_id": graph_id,
            "q": q,
            "source_type": source_type,
            "team_run_id": team_run_id,
            "team_id": team_id,
            "member_role": member_role,
            "producer_kind": producer_kind,
        }
        if not self.owned:
            raise GraphNotFound(str(graph_id))
        out = [r for (r, _c) in self.records.values() if r.graph_id == graph_id]
        if source_type:
            out = [r for r in out if r.source_type == source_type]
        if q:
            out = [r for r in out if q.lower() in (r.filename or "").lower()]
        if team_run_id:
            out = [r for r in out if r.team_run_id == team_run_id]
        if team_id:
            out = [r for r in out if r.team_id == team_id]
        if member_role:
            out = [r for r in out if r.member_role == member_role]
        if producer_kind:
            out = [r for r in out if r.producer_kind == producer_kind]
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


async def test_list_artifacts_forwards_every_provenance_filter(client, fake_service) -> None:
    """#728 added four provenance filters to the listing. The route must carry each one down to the
    service with the value the caller sent — parsed to its declared type, not as a raw string."""
    gid = uuid.uuid4()
    run = uuid.uuid4()
    resp = await client.get(
        f"/v1/artifacts?graph_id={gid}"
        f"&team_run_id={run}&team_id=writers-room&member_role=editor&producer_kind=agent",
        headers=_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert fake_service.last_call["graph_id"] == gid
    assert fake_service.last_call["team_run_id"] == run
    assert fake_service.last_call["team_id"] == "writers-room"
    assert fake_service.last_call["member_role"] == "editor"
    assert fake_service.last_call["producer_kind"] == "agent"


async def test_list_artifacts_without_filters_passes_none(client, fake_service) -> None:
    """Omitting the provenance filters must list the graph exactly as before #728 — every filter
    arrives as None, never as an empty string that would silently match nothing."""
    gid = uuid.uuid4()
    fake_service.add(_record(gid, filename="bible/canon.md"), "V1")
    resp = await client.get(f"/v1/artifacts?graph_id={gid}", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    for key in ("q", "source_type", "team_run_id", "team_id", "member_role", "producer_kind"):
        assert fake_service.last_call[key] is None, key


async def test_list_artifacts_provenance_filters_narrow_the_result(client, fake_service) -> None:
    """Each filter selects, and the unmatched artifacts drop out. Two agent artifacts from two runs
    of two teams, plus a plain user upload: each filter picks exactly its own."""
    gid = uuid.uuid4()
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    fake_service.add(
        _record(
            gid,
            filename="a.md",
            team_run_id=run_a,
            team_id="writers-room",
            member_role="editor",
            producer_kind="agent",
        ),
        "A",
    )
    fake_service.add(
        _record(
            gid,
            filename="b.md",
            team_run_id=run_b,
            team_id="research-pod",
            member_role="researcher",
            producer_kind="agent",
        ),
        "B",
    )
    fake_service.add(_record(gid, filename="upload.md", producer_kind="upload"), "U")

    async def _names(query: str) -> set[str]:
        resp = await client.get(f"/v1/artifacts?graph_id={gid}&{query}", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        return {a["filename"] for a in resp.json()}

    assert await _names(f"team_run_id={run_a}") == {"a.md"}
    assert await _names("team_id=research-pod") == {"b.md"}
    assert await _names("member_role=editor") == {"a.md"}
    assert await _names("producer_kind=agent") == {"a.md", "b.md"}
    assert await _names("producer_kind=upload") == {"upload.md"}


async def test_list_artifacts_rejects_a_non_uuid_team_run_id(client) -> None:
    """``team_run_id`` is a UUID on the route. A malformed one is a 422 from validation, never a
    string handed to the repository query."""
    resp = await client.get(
        f"/v1/artifacts?graph_id={uuid.uuid4()}&team_run_id=not-a-uuid", headers=_AUTH
    )
    assert resp.status_code == 422


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
        async def assert_readable(self, *, graph_id):
            return None

        async def get_graph(self, *, graph_id, user_id):
            return None

    svc = JobService(
        job_repo=_Repo(),  # type: ignore[arg-type]
        graph_service=_Graphs(),  # type: ignore[arg-type]
        enqueue=lambda *_a, **_k: None,
    )
    _job, content = await svc.get_artifact(artifact_id=rec.id)
    assert content == "# verbatim\nbody"
