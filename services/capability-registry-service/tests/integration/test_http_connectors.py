"""Integration: Notion + GitHub HTTP connectors via a mocked transport (S5b) — SaaS breadth proof.

The credential-resolution (api_key) + executor-dispatch seam runs end-to-end through the app; the
SaaS HTTP call is served by an httpx MockTransport (no live network), proving the connector builds
the request, parses a success response, and maps a non-200 to a failure. The live API call is
key-gated and opt-in (NOTION_API_KEY / GITHUB_TOKEN) — exercised manually, never in CI.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"


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


async def _instance(client: AsyncClient, tool_name: str) -> str:
    tools = (await client.get("/api/v1/tools", headers=_auth())).json()["capabilities"]
    cap_id = next(t["id"] for t in tools if t["name"] == tool_name)
    iid = (
        await client.post(
            "/api/v1/instances", json={"capability_id": cap_id, "name": "x"}, headers=_auth()
        )
    ).json()["id"]
    await client.post(
        f"/api/v1/instances/{iid}/configure-credentials",
        json={"credential_mappings": {"api_key": "cred-1"}},
        headers=_auth(),
    )
    return iid


async def test_notion_search_success_via_mock(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oraclous_capability_registry_service.domain.connectors import notion

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/search"
        assert req.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"results": [{"id": "p1"}]})

    monkeypatch.setattr(notion.NotionReader, "transport", httpx.MockTransport(handler))
    iid = await _instance(client, "Notion Reader")
    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "search", "query": "hi"}},
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["status"] == "SUCCESS"
    assert out["output_data"]["documents"]["results"] == [{"id": "p1"}]


async def test_notion_auth_failure_is_mapped(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oraclous_capability_registry_service.domain.connectors import notion

    monkeypatch.setattr(
        notion.NotionReader,
        "transport",
        httpx.MockTransport(lambda req: httpx.Response(401, json={"message": "unauthorized"})),
    )
    iid = await _instance(client, "Notion Reader")
    out = (
        await client.post(
            f"/api/v1/instances/{iid}/execute",
            json={"input_data": {"operation": "search"}},
            headers=_auth(),
        )
    ).json()
    # the seam ran end-to-end; the live call's 401 maps to a structured failure (FAILED, not 5xx)
    assert out["status"] == "FAILED"
    assert out["error_type"] == "NOTION_API_ERROR"


async def test_github_list_files_success_via_mock(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oraclous_capability_registry_service.domain.connectors import github

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/o/r/contents/"
        return httpx.Response(200, json=[{"name": "README.md", "type": "file"}])

    monkeypatch.setattr(github.GitHubReader, "transport", httpx.MockTransport(handler))
    iid = await _instance(client, "GitHub Reader")
    resp = await client.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "list_files", "repo": "o/r"}},
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["status"] == "SUCCESS"
    assert out["output_data"]["entries"][0]["name"] == "README.md"
