"""Unit: the public published-agent surface — binding enforcement + auth, through the app.

Drives create_app with fake repos on app.state and a real seeded integration key, so the real
get_edge_principal -> resolve -> require_bound_key chain runs. get_invoke_service is overridden with
a fake (the harness call is in test_invoke_service). Asserts: a key bound to the agent gets
through; a key bound to a DIFFERENT agent, a capability-only key, a member JWT, and no auth are all
rejected before any invoke; GET-by-slug returns only public metadata.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_application_gateway_service.app.factory import create_app
from oraclous_application_gateway_service.core.dependencies import get_invoke_service
from oraclous_application_gateway_service.domain.integration_key import mint_key
from oraclous_application_gateway_service.schema.invoke_schemas import InvokeResponse

pytestmark = pytest.mark.unit

_DEV_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")


class _FakeKeys:
    def __init__(self) -> None:
        self.rows: list = []

    async def get_by_prefix(self, key_prefix):  # noqa: ANN001
        return next((r for r in self.rows if r.key_prefix == key_prefix), None)


class _FakeAgents:
    def __init__(self) -> None:
        self.rows: list = []

    async def get_by_slug(self, *, organisation_id, slug):  # noqa: ANN001
        return next(
            (r for r in self.rows if r.organisation_id == organisation_id and r.slug == slug), None
        )


class _FakeInvoke:
    async def invoke(self, *, slug, agent_input, principal):  # noqa: ANN001
        return InvokeResponse(
            execution_id=uuid.uuid4(), status="SUCCEEDED", output="ok", error=None
        )


def _seed_key(keys, *, bound_slug):  # noqa: ANN001 -> the plaintext
    m = mint_key("oak")
    keys.rows.append(
        SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=_DEV_ORG,
            key_prefix=m.key_prefix,
            key_hash=m.key_hash,
            last4=m.last4,
            bound_agent_slug=bound_slug,
            capability_allow_list=None,
            cors_origins=None,
            status="active",
            expires_at=None,
        )
    )
    return m.plaintext


def _app():
    from oraclous_application_gateway_service.core.config import get_settings

    get_settings.cache_clear()
    app = create_app(lifespan=None)
    app.state.integration_key_repo = _FakeKeys()
    app.state.published_agent_repo = _FakeAgents()
    app.state.published_agent_repo.rows.append(
        SimpleNamespace(
            organisation_id=_DEV_ORG,
            slug="weather",
            bound_capability_ref="cap-1",
            display_name="Weather",
            description="forecasts",
            status="active",
        )
    )
    app.dependency_overrides[get_invoke_service] = _FakeInvoke
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test")


def _bearer(tok):
    return {"authorization": f"Bearer {tok}"}


async def test_bound_key_can_invoke() -> None:
    app = _app()
    tok = _seed_key(app.state.integration_key_repo, bound_slug="weather")
    async with _client(app) as c:
        r = await c.post("/v1/agents/weather/invoke", json={"input": "hi"}, headers=_bearer(tok))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "SUCCEEDED"


async def test_key_bound_to_other_agent_is_403() -> None:
    app = _app()
    tok = _seed_key(app.state.integration_key_repo, bound_slug="weather")
    async with _client(app) as c:
        r = await c.post("/v1/agents/other/invoke", json={"input": "hi"}, headers=_bearer(tok))
    assert r.status_code == 403


async def test_capability_only_key_cannot_invoke() -> None:
    app = _app()
    tok = _seed_key(app.state.integration_key_repo, bound_slug=None)  # a capability-bound key
    async with _client(app) as c:
        r = await c.post("/v1/agents/weather/invoke", json={"input": "hi"}, headers=_bearer(tok))
    assert r.status_code == 403


async def test_member_jwt_cannot_invoke() -> None:
    app = _app()
    async with _client(app) as c:
        r = await c.post(
            "/v1/agents/weather/invoke", json={"input": "hi"}, headers=_bearer("dev-token")
        )
    assert r.status_code == 403


async def test_no_auth_is_401() -> None:
    app = _app()
    async with _client(app) as c:
        r = await c.post("/v1/agents/weather/invoke", json={"input": "hi"})
    assert r.status_code == 401


async def test_get_metadata_is_public_projection() -> None:
    app = _app()
    tok = _seed_key(app.state.integration_key_repo, bound_slug="weather")
    async with _client(app) as c:
        r = await c.get("/v1/agents/weather", headers=_bearer(tok))
    assert r.status_code == 200
    body = r.json()
    assert body == {"slug": "weather", "display_name": "Weather", "description": "forecasts"}
    # no internal fields leak
    assert "bound_capability_ref" not in body and "organisation_id" not in body


async def test_get_metadata_wrong_binding_is_403() -> None:
    app = _app()
    tok = _seed_key(app.state.integration_key_repo, bound_slug="other")
    async with _client(app) as c:
        r = await c.get("/v1/agents/weather", headers=_bearer(tok))
    assert r.status_code == 403
