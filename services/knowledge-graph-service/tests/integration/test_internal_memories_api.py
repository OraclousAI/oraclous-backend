"""Internal agent-memory write HTTP layer (#332 / ADR-027 §5) — POST /internal/v1/memories.

The harness post-run memory hook (ADR-018 internal-key path). No Postgres/Neo4j:
``get_memory_service`` is overridden with a fake that records the store calls and the resolved
graph. The decisive checks:

  * dev-auth mode: auth is required (401 with no bearer);
  * GATEWAY mode: the X-Internal-Key gate is fail-closed — a missing OR empty key is 403 even with
    valid identity headers (the route reaches the gate through ``get_principal``);
  * the org is bound from the FORWARDED principal — the body never carries an org;
  * default-graph fallback: a body with NO graph_id resolves the org's system agent-memory graph;
  * a body graph_id NOT in the principal's org is rejected (404 — a request can't write into
    another org's graph).
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.dependencies import get_memory_service
from oraclous_knowledge_graph_service.schema.memory_schemas import MemoryCreateResponse
from oraclous_knowledge_graph_service.services.memory_service import GraphNotVisible

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}
_DEFAULT_GRAPH = uuid.uuid4()
_FOREIGN_GRAPH = uuid.uuid4()  # a graph NOT in the principal's org

# gateway-mode identity headers (the verified principal the gateway forwards).
_PRINCIPAL = "11111111-1111-1111-1111-111111111111"
_ORG = "22222222-2222-2222-2222-222222222222"
_KEY = "test-internal-key"  # noqa: S105 — test attestation key


class _FakeMemoryService:
    """Records what the route asked of the service. ``foreign_graphs`` are treated as not visible
    in the principal's org (the org gate → GraphNotVisible → 404)."""

    def __init__(self) -> None:
        self.stored: list[dict] = []
        self.resolved_default_for: list[uuid.UUID] = []
        self.foreign_graphs: set[uuid.UUID] = {_FOREIGN_GRAPH}

    async def resolve_default_graph(self, *, user_id: uuid.UUID) -> uuid.UUID:
        self.resolved_default_for.append(user_id)
        return _DEFAULT_GRAPH

    async def store(self, *, graph_id: uuid.UUID, req) -> MemoryCreateResponse:
        if graph_id in self.foreign_graphs:
            raise GraphNotVisible(str(graph_id))
        self.stored.append({"graph_id": graph_id, "type": req.type.value, "scope": req.scope.value})
        return MemoryCreateResponse(memory_id="mem-1", importance_score=0.4)


@pytest.fixture
def fake_service() -> _FakeMemoryService:
    return _FakeMemoryService()


@pytest.fixture
def client(app, async_client, fake_service):
    app.dependency_overrides[get_memory_service] = lambda: fake_service
    yield async_client
    app.dependency_overrides.clear()


@pytest.fixture
def gateway_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KGS_AUTH_MODE", "gateway")
    monkeypatch.setenv("KGS_INTERNAL_SERVICE_KEY", _KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_EPISODIC = {"type": "episodic", "content": "Run SUCCEEDED", "scope": "agent"}


async def test_requires_auth_in_dev_mode(client) -> None:
    resp = await client.post("/internal/v1/memories", json=_EPISODIC)
    assert resp.status_code == 401


async def test_default_graph_fallback_when_no_graph_id(client, fake_service) -> None:
    resp = await client.post("/internal/v1/memories", json=_EPISODIC, headers=_AUTH)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # the memory landed in the resolved org-default (system) graph, named in the response
    assert body["graph_id"] == str(_DEFAULT_GRAPH)
    assert len(fake_service.resolved_default_for) == 1
    assert fake_service.stored[0]["graph_id"] == _DEFAULT_GRAPH
    # ORG001: the org id is scope, never a response/echo field
    assert "organisation_id" not in resp.text


async def test_explicit_graph_id_in_org_is_used(client, fake_service) -> None:
    gid = uuid.uuid4()  # an in-org graph
    resp = await client.post(
        "/internal/v1/memories", json={**_EPISODIC, "graph_id": str(gid)}, headers=_AUTH
    )
    assert resp.status_code == 201, resp.text
    assert fake_service.stored[0]["graph_id"] == gid
    assert fake_service.resolved_default_for == []  # an explicit graph skips the default fallback


async def test_cross_org_body_graph_id_is_rejected(client, fake_service) -> None:
    # a body graph_id that is not in the principal's org → the org gate → 404 (no cross-org write).
    resp = await client.post(
        "/internal/v1/memories",
        json={**_EPISODIC, "graph_id": str(_FOREIGN_GRAPH)},
        headers=_AUTH,
    )
    assert resp.status_code == 404
    assert fake_service.stored == []  # nothing was written


async def test_gateway_mode_missing_internal_key_is_403(client, gateway_env) -> None:
    # valid identity headers but NO X-Internal-Key → fail-closed 403 (request not from the gateway).
    resp = await client.post(
        "/internal/v1/memories",
        json=_EPISODIC,
        headers={
            "X-Principal-Id": _PRINCIPAL,
            "X-Principal-Type": "agent",
            "X-Organisation-Id": _ORG,
        },
    )
    assert resp.status_code == 403


async def test_gateway_mode_empty_internal_key_is_403(client, gateway_env) -> None:
    resp = await client.post(
        "/internal/v1/memories",
        json=_EPISODIC,
        headers={
            "X-Principal-Id": _PRINCIPAL,
            "X-Principal-Type": "agent",
            "X-Organisation-Id": _ORG,
            "X-Internal-Key": "",
        },
    )
    assert resp.status_code == 403
