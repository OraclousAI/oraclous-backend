"""Integration: every error the gateway emits conforms to the ORA-37 contract (ORA-54).

Drives each gateway error path (own 404, edge 401, upstream-unavailable 502, upstream-timeout 504,
and a normalised upstream 4xx) through the real app and asserts each response body validates against
the schema, trips no forbidden-substring pattern, and sets ``X-Request-Id`` equal to ``requestId``.

Marked ``security`` because the dominant §3 risk is a sensitive-data leak in an error body.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from tools.contract.error_envelope import load_schema, scan_forbidden

pytestmark = [pytest.mark.integration, pytest.mark.security]

_VALIDATOR = Draft202012Validator(load_schema())


async def _ok(request):  # noqa: ANN001 — starlette upstream handler
    return JSONResponse({"ok": True})


async def _upstream_403(request):  # noqa: ANN001 — leaky body the gateway must not relay
    return JSONResponse({"detail": "denied for user on db-1.internal 10.1.2.3"}, status_code=403)


_UPSTREAM = Starlette(
    routes=[
        Route("/v1/search", _ok, methods=["GET"]),
        Route("/api/v1/capabilities", _upstream_403, methods=["GET"]),
    ]
)


def _gateway(transport: httpx.AsyncBaseTransport):
    from oraclous_application_gateway_service.app.factory import create_app
    from oraclous_application_gateway_service.core.config import get_settings
    from oraclous_application_gateway_service.domain.route_table import build_route_table
    from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
    from oraclous_application_gateway_service.services.proxy_service import ProxyService

    get_settings.cache_clear()
    app = create_app(lifespan=None)
    upstream = httpx.AsyncClient(transport=transport)
    app.state.http_client = upstream
    table = build_route_table(get_settings())
    app.state.route_table = table
    app.state.proxy_service = ProxyService(
        route_table=table, upstream_client=UpstreamClient(upstream), internal_key="ik-test"
    )
    return app, upstream


def _auth() -> dict:
    return {"Authorization": "Bearer dev-token"}


def _assert_conformant(response: httpx.Response) -> None:
    errors = [e.message for e in _VALIDATOR.iter_errors(response.json())]
    assert not errors, errors
    assert scan_forbidden(response.text) == []
    assert response.headers.get("x-request-id") == response.json()["error"]["requestId"]


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app, upstream = _gateway(ASGITransport(app=_UPSTREAM))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        yield c
    await upstream.aclose()


async def test_own_404_is_conformant(client: AsyncClient) -> None:
    r = await client.get("/totally/unknown", headers=_auth())
    assert r.status_code == 404
    _assert_conformant(r)


async def test_edge_401_is_conformant(client: AsyncClient) -> None:
    r = await client.get("/v1/search")  # no token
    assert r.status_code == 401
    _assert_conformant(r)


async def test_normalised_upstream_error_is_conformant(client: AsyncClient) -> None:
    r = await client.get("/api/v1/capabilities", headers=_auth())
    assert r.status_code == 403
    _assert_conformant(r)  # also proves the leaky upstream body did not survive


async def test_connect_failure_is_conformant() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    app, upstream = _gateway(httpx.MockTransport(boom))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        r = await c.get("/v1/search", headers=_auth())
    await upstream.aclose()
    assert r.status_code == 502
    _assert_conformant(r)


async def test_timeout_is_conformant() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    app, upstream = _gateway(httpx.MockTransport(slow))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        r = await c.get("/v1/search", headers=_auth())
    await upstream.aclose()
    assert r.status_code == 504
    _assert_conformant(r)


class _BoomProxy:
    """A proxy service whose open_upstream raises a non-domain exception — exercises the catch-all
    ``@app.exception_handler(Exception)`` 500 path (which runs OUTSIDE RequestIdMiddleware)."""

    async def open_upstream(self, **_kwargs: object) -> httpx.Response:
        raise RuntimeError("non-domain boom")


async def test_unhandled_exception_is_conformant() -> None:
    from oraclous_application_gateway_service.app.factory import create_app
    from oraclous_application_gateway_service.core.config import get_settings

    get_settings.cache_clear()
    app = create_app(lifespan=None)
    app.state.proxy_service = _BoomProxy()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://gw.test") as c:
        r = await c.get("/v1/search", headers=_auth())
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"
    # the catch-all 500 must still carry X-Request-Id == body.requestId (regression guard)
    _assert_conformant(r)
