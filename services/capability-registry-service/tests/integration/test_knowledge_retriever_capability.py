"""Integration: the first-party knowledge-retriever capability (closes the Wave-1 ref gap).

End-to-end through the app on a real Postgres: the seeded ``core/knowledge-retriever@1.0.0`` tool is
listed by ``GET /api/v1/tools``, an OHM-style ``capabilities[].ref`` resolves to it by name-slug
(the same resolution the harness does, so an OHM manifest no longer 422s), and invoking its
``search`` operation calls the retriever (the outbound HTTP stubbed — no live retriever) and returns
the hits. Org-scoping is proven by the org the connector forwards to the retriever.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    """Mirror the harness RegistryClient slug (lowercase, collapse non-alnum to '-')."""
    return _NON_ALNUM.sub("-", text.lower()).strip("-")


def _ref_slug(ref: str) -> str:
    """``core/knowledge-retriever@1.0.0`` → ``knowledge-retriever`` (drop prefix + @version)."""
    return _slug(ref.split("/")[-1].split("@")[0])


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DEV_BEARER", "dev-token")
    monkeypatch.setenv("DEV_ORG_ID", _DEV_ORG)
    from oraclous_capability_registry_service.core.config import get_settings

    get_settings.cache_clear()

    from oraclous_capability_registry_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from oraclous_capability_registry_service.app.factory import create_app
    from oraclous_capability_registry_service.repositories.capability_repository import (
        CapabilityRepository,
    )
    from oraclous_capability_registry_service.repositories.execution_repository import (
        ExecutionRepository,
    )
    from oraclous_capability_registry_service.repositories.instance_repository import (
        InstanceRepository,
    )
    from oraclous_capability_registry_service.services.credential_client import FakeCredentialBroker
    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

    app = create_app(lifespan=None)
    repo = CapabilityRepository(async_dsn)
    app.state.capability_repository = repo
    app.state.instance_repository = InstanceRepository(async_dsn)
    app.state.execution_repository = ExecutionRepository(async_dsn)
    app.state.credential_broker = FakeCredentialBroker(fake_db_dsn="unused")
    await sync_plugins(repository=repo, organisation_id=uuid.UUID(_DEV_ORG))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        c.repos = (repo, app.state.instance_repository, app.state.execution_repository)  # type: ignore[attr-defined]
        yield c
    for r in c.repos:  # type: ignore[attr-defined]
        await r.close()
    get_settings.cache_clear()


def _auth() -> dict:
    return {"Authorization": "Bearer dev-token"}


async def test_retriever_capability_is_listed_and_credential_free(client: AsyncClient) -> None:
    tools = (await client.get("/api/v1/tools", headers=_auth())).json()["capabilities"]
    retriever = next((t for t in tools if t["name"] == "Knowledge Retriever"), None)
    assert retriever is not None, "core/knowledge-retriever@1.0.0 must be registered"
    spec = retriever["descriptor"]["spec"]
    assert spec["type"] == "INTERNAL"
    assert spec["credential_requirements"] == []  # first-party: NO credential requirement
    assert any(c["name"] == "search" for c in spec["capabilities"])


async def test_an_ohm_ref_resolves_to_the_retriever(client: AsyncClient) -> None:
    """The harness resolves ``capabilities[].ref`` by name-slug; the seeded tool must match."""
    tools = (await client.get("/api/v1/tools", headers=_auth())).json()["capabilities"]
    ref = "core/knowledge-retriever@1.0.0"
    found = next((t for t in tools if _slug(t["name"]) == _ref_slug(ref)), None)
    assert found is not None, f"OHM ref {ref!r} must resolve (slug {_ref_slug(ref)!r})"
    assert found["name"] == "Knowledge Retriever"


async def test_invoking_search_calls_the_retriever_and_returns_hits(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oraclous_capability_registry_service.domain.connectors import knowledge_retriever

    graph_id = "22222222-2222-2222-2222-222222222222"
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["org"] = req.headers.get("X-Organisation-Id")  # dev mode → None (bearer instead)
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(
            200, json=[{"id": "n1", "type": "Chunk", "properties": {"text": "hello"}}]
        )

    monkeypatch.setattr(
        knowledge_retriever.KnowledgeRetrieverConnector,
        "transport",
        httpx.MockTransport(handler),
    )

    tools = (await client.get("/api/v1/tools", headers=_auth())).json()["capabilities"]
    cap_id = next(t["id"] for t in tools if t["name"] == "Knowledge Retriever")
    iid = (
        await client.post(
            "/api/v1/instances",
            json={"capability_id": cap_id, "name": "qa-graph"},
            headers=_auth(),
        )
    ).json()["id"]

    # NO configure-credentials step is needed — the tool declares no credential requirement.
    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"graph_id": graph_id, "query": "what is hello"}},
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["status"] == "SUCCESS", out
    assert out["output_data"]["hits"] == [
        {"id": "n1", "type": "Chunk", "properties": {"text": "hello"}}
    ]
    assert seen["path"] == "/v1/search/semantic"
    # dev mode forwards a bearer (the retriever resolves it to the shared dev org → org-scoped)
    assert seen["auth"] == "Bearer dev-token"


async def test_search_without_a_credential_mapping_is_not_a_readiness_failure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first-party tool must run with no credential mapped — never a 409 'not ready'."""
    from oraclous_capability_registry_service.domain.connectors import knowledge_retriever

    monkeypatch.setattr(
        knowledge_retriever.KnowledgeRetrieverConnector,
        "transport",
        httpx.MockTransport(lambda _r: httpx.Response(200, json=[])),
    )
    tools = (await client.get("/api/v1/tools", headers=_auth())).json()["capabilities"]
    cap_id = next(t["id"] for t in tools if t["name"] == "Knowledge Retriever")
    iid = (
        await client.post(
            "/api/v1/instances", json={"capability_id": cap_id, "name": "x"}, headers=_auth()
        )
    ).json()["id"]
    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"graph_id": "33333333-3333-3333-3333-333333333333", "query": "q"}},
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text  # not 409 — no credential required
    assert resp.json()["status"] == "SUCCESS"
