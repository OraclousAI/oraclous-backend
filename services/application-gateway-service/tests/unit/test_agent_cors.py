"""Unit: per-key CORS through the full app — preflight, per-key ACAO on the response, scoping.

Drives create_app with a fake key repo (a key carrying cors_origins) + an overridden invoke service,
so the real AgentCorsMiddleware (outside the gateway CORS) runs. Asserts: the agent preflight is
answered without credentials; the actual response reflects ACAO only for a listed origin (exactly
one, no credentials); an unlisted origin / a no-policy key gets none; non-agent paths are untouched.
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
_GOOD = "https://good.example"
_EVIL = "https://evil.example"


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
            execution_id=uuid.uuid4(), status="succeeded", output="ok", error=None
        )


def _seed_key(keys, *, cors_origins):  # noqa: ANN001 -> plaintext
    m = mint_key("oak")
    keys.rows.append(
        SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=_DEV_ORG,
            key_prefix=m.key_prefix,
            key_hash=m.key_hash,
            last4=m.last4,
            bound_agent_slug="weather",
            capability_allow_list=None,
            cors_origins=cors_origins,
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
            display_name="W",
            description="d",
            status="active",
        )
    )
    app.dependency_overrides[get_invoke_service] = _FakeInvoke
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test")


async def test_preflight_is_answered_without_credentials() -> None:
    app = _app()
    async with _client(app) as c:
        r = await c.options(
            "/v1/agents/weather/invoke",
            headers={"Origin": _GOOD, "Access-Control-Request-Method": "POST"},
        )
    assert r.status_code == 204
    assert r.headers.get("access-control-allow-origin") == _GOOD
    assert "POST" in r.headers.get("access-control-allow-methods", "")
    assert "access-control-allow-credentials" not in r.headers  # never on this plane


async def test_listed_origin_gets_exactly_one_acao_no_credentials() -> None:
    app = _app()
    tok = _seed_key(app.state.integration_key_repo, cors_origins=[_GOOD])
    async with _client(app) as c:
        r = await c.post(
            "/v1/agents/weather/invoke",
            json={"input": "hi"},
            headers={"Origin": _GOOD, "Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 200
    assert r.headers.get_list("access-control-allow-origin") == [_GOOD]  # exactly one
    assert "access-control-allow-credentials" not in r.headers


async def test_unlisted_origin_gets_no_acao() -> None:
    app = _app()
    tok = _seed_key(app.state.integration_key_repo, cors_origins=[_GOOD])
    async with _client(app) as c:
        r = await c.post(
            "/v1/agents/weather/invoke",
            json={"input": "hi"},
            headers={"Origin": _EVIL, "Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 200  # the invoke still ran (the key gates execution)
    assert "access-control-allow-origin" not in r.headers  # but the browser can't read it


async def test_key_with_no_cors_policy_is_fail_closed() -> None:
    app = _app()
    tok = _seed_key(app.state.integration_key_repo, cors_origins=None)
    async with _client(app) as c:
        r = await c.post(
            "/v1/agents/weather/invoke",
            json={"input": "hi"},
            headers={"Origin": _GOOD, "Authorization": f"Bearer {tok}"},
        )
    assert "access-control-allow-origin" not in r.headers


async def test_metadata_get_also_per_key_scoped() -> None:
    app = _app()
    tok = _seed_key(app.state.integration_key_repo, cors_origins=[_GOOD])
    async with _client(app) as c:
        r = await c.get(
            "/v1/agents/weather", headers={"Origin": _GOOD, "Authorization": f"Bearer {tok}"}
        )
    assert r.headers.get("access-control-allow-origin") == _GOOD


async def test_non_agent_path_is_untouched_by_per_key_middleware() -> None:
    # a member-plane preflight is answered by the GATEWAY-WIDE Starlette CORS (200), NOT AgentCors
    # (which answers agent preflights with a 204) — proving AgentCors is scoped to the agent paths.
    app = _app()
    async with _client(app) as c:
        r = await c.options(
            "/v1/integration-keys",
            headers={"Origin": _GOOD, "Access-Control-Request-Method": "POST"},
        )
    assert r.status_code == 200 and "access-control-allow-origin" in r.headers  # the global CORS


async def test_gateway_wide_cors_does_not_advertise_credentials() -> None:
    # header-auth (Bearer) not cookies -> the gateway-wide CORS must NOT set allow-credentials (else
    # ["*"] becomes reflect-any-origin-with-credentials). A non-agent response reflects the posture.
    app = _app()
    async with _client(app) as c:
        r = await c.get("/health", headers={"Origin": _EVIL})
    assert "access-control-allow-credentials" not in r.headers
