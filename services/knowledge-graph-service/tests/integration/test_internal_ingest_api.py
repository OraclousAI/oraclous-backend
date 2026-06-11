"""Internal ingest HTTP layer (Slice C) — the agent-addressable POST /internal/v1/ingest.

The write twin of the internal SEARCH the retriever calls (ADR-018). No Postgres/Neo4j/broker:
``get_job_service`` is overridden with a fake. The decisive checks: auth is required (401 with no
bearer); ingestion is enqueued via the SAME JobService.submit the user-facing route uses; the org
is bound from the principal (the body never carries an org); a graph not in the principal's org is a
404 (the owner gate inside submit). Gateway-mode X-Internal-Key gating is covered at the dependency
level in tests/unit/test_gateway_mode_auth.py (the route reuses get_principal verbatim).
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
_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")


def _record(graph_id: uuid.UUID, source_type: str = "text") -> IngestionJobRecord:
    now = datetime(2026, 6, 4, tzinfo=UTC)
    return IngestionJobRecord(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        graph_id=graph_id,
        source_type=source_type,
        filename="inline.txt",
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
        self.owned = True
        self.submitted: list[dict] = []

    async def submit(self, *, user_id, graph_id, data, filename, source_type, recipe_id=None, **_):
        if not self.owned:
            raise GraphNotFound(str(graph_id))
        self.submitted.append(
            {
                "user_id": user_id,
                "graph_id": graph_id,
                "data": data,
                "source_type": source_type,
                "recipe_id": recipe_id,
            }
        )
        return _record(graph_id, source_type)


@pytest.fixture
def fake_service() -> _FakeJobService:
    return _FakeJobService()


@pytest.fixture
def client(app, async_client, fake_service):
    app.dependency_overrides[get_job_service] = lambda: fake_service
    yield async_client
    app.dependency_overrides.clear()


async def test_internal_ingest_requires_internal_key_or_auth(client) -> None:
    # dev-auth mode: no bearer → 401 (the gateway-mode X-Internal-Key gate is the twin gate)
    resp = await client.post(
        "/internal/v1/ingest", json={"graph_id": str(uuid.uuid4()), "content": "hi"}
    )
    assert resp.status_code == 401


async def test_internal_ingest_enqueues_and_is_org_scoped(client, fake_service) -> None:
    gid = uuid.uuid4()
    resp = await client.post(
        "/internal/v1/ingest",
        json={"graph_id": str(gid), "content": "hello world"},
        headers=_AUTH,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["source_type"] == "text"
    # org is NEVER echoed (ORG001) — it is bound from the principal, not the body
    assert "organisation_id" not in body
    # the SAME JobService.submit was driven (one enqueue), scoped to the graph in the body
    assert len(fake_service.submitted) == 1
    submitted = fake_service.submitted[0]
    assert submitted["graph_id"] == gid
    assert submitted["data"] == b"hello world"


async def test_internal_ingest_accepts_source_content_alias(client, fake_service) -> None:
    gid = uuid.uuid4()
    resp = await client.post(
        "/internal/v1/ingest",
        json={
            "graph_id": str(gid),
            "source_content": '[{"id": 1}]',
            "source_type": "json",
            "recipe_id": "rcp_x",
        },
        headers=_AUTH,
    )
    assert resp.status_code == 202, resp.text
    submitted = fake_service.submitted[0]
    assert submitted["source_type"] == "json"
    assert submitted["recipe_id"] == "rcp_x"
    assert submitted["data"] == b'[{"id": 1}]'


async def test_internal_ingest_rejects_graph_not_in_principal_org(client, fake_service) -> None:
    # the owner gate inside submit is org-scoped: a graph not in the principal's org → GraphNotFound
    fake_service.owned = False
    resp = await client.post(
        "/internal/v1/ingest",
        json={"graph_id": str(uuid.uuid4()), "content": "hi"},
        headers=_AUTH,
    )
    assert resp.status_code == 404


async def test_internal_ingest_empty_content_is_422(client) -> None:
    resp = await client.post(
        "/internal/v1/ingest",
        json={"graph_id": str(uuid.uuid4()), "content": ""},
        headers=_AUTH,
    )
    assert resp.status_code == 422
