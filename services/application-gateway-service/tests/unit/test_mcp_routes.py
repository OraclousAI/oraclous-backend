"""Unit: the /v1/mcp route — integration-key auth (member JWT -> 403) + JSON-RPC over HTTP (S8)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_application_gateway_service.app.factory import create_app
from oraclous_application_gateway_service.domain.integration_key import mint_key

pytestmark = pytest.mark.unit

_DEV_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")


class _FakeKeys:
    def __init__(self, row) -> None:  # noqa: ANN001
        self._row = row

    async def get_by_prefix(self, key_prefix):  # noqa: ANN001
        return self._row if self._row.key_prefix == key_prefix else None


class _FakeAgents:
    async def get_by_slug(self, *, organisation_id, slug):  # noqa: ANN001
        if slug == "weather" and organisation_id == _DEV_ORG:
            return SimpleNamespace(
                slug="weather",
                bound_capability_ref="cap-w",
                display_name="W",
                description=None,
                status="active",
            )
        return None

    async def list_for_org(self, organisation_id):  # noqa: ANN001
        return []


def _app_with_key():  # noqa: ANN202
    from oraclous_application_gateway_service.core.config import get_settings

    get_settings.cache_clear()
    app = create_app(lifespan=None)
    minted = mint_key("oak")
    app.state.integration_key_repo = _FakeKeys(
        SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=_DEV_ORG,
            key_prefix=minted.key_prefix,
            key_hash=minted.key_hash,
            status="active",
            expires_at=None,
            bound_agent_slug="weather",
            capability_allow_list=None,
            cors_origins=None,
        )
    )
    # the pre-auth get_by_prefix producer reads the OWNER-engine repo (ADR-030 §3); a fake has no
    # RLS so the same instance serves both.
    app.state.integration_key_owner_repo = app.state.integration_key_repo
    app.state.published_agent_repo = _FakeAgents()
    app.state.http_client = httpx.AsyncClient()  # the invoke service is built but unused here
    return app, {"authorization": f"Bearer {minted.plaintext}"}


def _client(app):  # noqa: ANN001
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test")


async def test_initialize_with_an_integration_key() -> None:
    app, hdr = _app_with_key()
    async with _client(app) as c:
        r = await c.post(
            "/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}, headers=hdr
        )
    assert r.status_code == 200 and r.json()["result"]["protocolVersion"]


async def test_tools_list_scoped_to_the_key_binding() -> None:
    app, hdr = _app_with_key()
    async with _client(app) as c:
        r = await c.post(
            "/v1/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=hdr
        )
    assert r.status_code == 200
    assert [t["name"] for t in r.json()["result"]["tools"]] == ["weather"]


async def test_a_member_jwt_is_forbidden() -> None:
    app, _ = _app_with_key()
    async with _client(app) as c:
        r = await c.post(
            "/v1/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"authorization": "Bearer dev-token"},
        )
    assert r.status_code == 403  # MCP is a programmatic-client door, not a member-console door


async def test_no_bearer_is_unauthenticated() -> None:
    app, _ = _app_with_key()
    async with _client(app) as c:
        r = await c.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401


async def test_a_notification_returns_202() -> None:
    app, hdr = _app_with_key()
    async with _client(app) as c:
        r = await c.post(
            "/v1/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=hdr
        )
    assert r.status_code == 202
