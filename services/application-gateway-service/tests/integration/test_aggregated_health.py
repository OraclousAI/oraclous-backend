"""Integration: aggregated upstream health + gateway own-error envelope (GW-5).

The gateway's upstream client uses a MockTransport that answers ``/health`` 200 for some upstreams
and refuses others; ``GET /health/upstreams`` rolls them up (HTTP 200, body reflects degraded). The
gateway's own errors (404/401) carry the canonical ORA-37 envelope
``{"error": {"code", "message", "requestId", "retryable"}}`` with a server-minted ``requestId``
echoed in ``X-Request-Id``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_REQUEST_ID = re.compile(r"^req_[0-9A-Za-z]+$")


def _health_handler(down_host: str | None):
    def handler(request: httpx.Request) -> httpx.Response:
        if down_host and request.url.host == down_host:
            raise httpx.ConnectError("refused")
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(404)

    return handler


def _gateway(down_host: str | None):
    from oraclous_application_gateway_service.app.factory import create_app
    from oraclous_application_gateway_service.core.config import get_settings
    from oraclous_application_gateway_service.domain.route_table import build_route_table
    from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
    from oraclous_application_gateway_service.services.proxy_service import ProxyService

    get_settings.cache_clear()
    app = create_app(lifespan=None)
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(_health_handler(down_host)))
    app.state.http_client = upstream
    table = build_route_table(get_settings())
    app.state.route_table = table
    app.state.proxy_service = ProxyService(
        route_table=table, upstream_client=UpstreamClient(upstream), internal_key="ik-test"
    )
    return app, upstream


@pytest.fixture
async def all_up() -> AsyncIterator[AsyncClient]:
    app, upstream = _gateway(down_host=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        yield c
    await upstream.aclose()


async def test_all_upstreams_up_rolls_up_to_ok(all_up: AsyncClient) -> None:
    r = await all_up.get("/health/upstreams")
    assert r.status_code == 200
    body = r.json()
    assert body["overall"] == "ok"
    assert len(body["upstreams"]) == 7
    assert all(u["status"] == "ok" for u in body["upstreams"])


async def test_one_upstream_down_rolls_up_to_degraded() -> None:
    app, upstream = _gateway(down_host="knowledge-retriever-service")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        r = await c.get("/health/upstreams")
    await upstream.aclose()
    assert r.status_code == 200  # the endpoint itself stays 200; the body reflects degraded
    body = r.json()
    assert body["overall"] == "degraded"
    krs = next(u for u in body["upstreams"] if u["name"] == "knowledge-retriever")
    assert krs["status"] == "down"


async def test_gateway_404_carries_the_error_envelope(all_up: AsyncClient) -> None:
    r = await all_up.get("/totally/unknown", headers={"Authorization": "Bearer dev-token"})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "NOT_FOUND"
    assert err["message"]
    assert err["retryable"] is False
    assert _REQUEST_ID.match(err["requestId"])
    assert r.headers["x-request-id"] == err["requestId"]


async def test_client_request_id_is_not_trusted(all_up: AsyncClient) -> None:
    # A client-supplied X-Request-Id is never echoed as the requestId (anti-spoof): the gateway
    # mints its own server-authoritative, contract-shaped id instead.
    r = await all_up.get(
        "/totally/unknown",
        headers={"Authorization": "Bearer dev-token", "X-Request-Id": "rid-123"},
    )
    err = r.json()["error"]
    assert err["requestId"] != "rid-123"
    assert _REQUEST_ID.match(err["requestId"])
    assert r.headers["x-request-id"] == err["requestId"]


async def test_edge_401_carries_the_error_envelope(all_up: AsyncClient) -> None:
    r = await all_up.get("/v1/search")  # no token
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "UNAUTHENTICATED"
    assert _REQUEST_ID.match(err["requestId"])
    # WWW-Authenticate: Bearer must survive the envelope (it was dropped before the fix)
    assert r.headers.get("www-authenticate") == "Bearer"
