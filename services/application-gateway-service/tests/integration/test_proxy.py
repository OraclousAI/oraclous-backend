"""Integration: the reverse-proxy forwards to the upstream + fails closed (GW-2).

The gateway app runs via ASGITransport; its internal upstream client is pointed at a real mock
upstream ASGI app (also via ASGITransport, which streams correctly) for the forward/passthrough
proofs, and at an httpx MockTransport that raises for the connect/timeout proofs. Proves: a routed
request is forwarded and the upstream's status/body stream back; the upstream's own status passes
through; ``/health`` is not proxied; an unknown prefix → gateway 404; connect failure → 502;
timeout → 504. The real cross-service forward is also proven by the docker smoke.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

pytestmark = pytest.mark.integration


async def _search(request):  # noqa: ANN001 — starlette handler
    return JSONResponse({"results": ["r1"], "via": "krs", "echo_q": request.url.query})


async def _caps(request):  # noqa: ANN001 — starlette handler
    return JSONResponse({"detail": "missing bearer"}, status_code=401)


# a real ASGI upstream (streams properly under ASGITransport, unlike MockTransport)
_UPSTREAM_APP = Starlette(
    routes=[
        Route("/v1/search", _search, methods=["GET"]),
        Route("/api/v1/capabilities", _caps, methods=["GET"]),
    ]
)


def _gateway_with(transport: httpx.AsyncBaseTransport):
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
        route_table=table, upstream_client=UpstreamClient(upstream)
    )
    return app, upstream


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app, upstream = _gateway_with(ASGITransport(app=_UPSTREAM_APP))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        yield c
    await upstream.aclose()


async def test_routed_request_is_forwarded_and_response_streams_back(client: AsyncClient) -> None:
    r = await client.get("/v1/search?q=hello")
    assert r.status_code == 200
    body = r.json()
    assert body["via"] == "krs"
    assert body["results"] == ["r1"]
    assert body["echo_q"] == "q=hello"  # query string forwarded verbatim


async def test_upstream_status_passes_through(client: AsyncClient) -> None:
    # the capability-registry upstream returns 401 (no bearer) — the gateway passes it through
    r = await client.get("/api/v1/capabilities")
    assert r.status_code == 401
    assert r.json()["detail"] == "missing bearer"


async def test_health_is_not_proxied(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "application-gateway"


async def test_unknown_prefix_is_gateway_404(client: AsyncClient) -> None:
    r = await client.get("/totally/unknown")
    assert r.status_code == 404
    assert r.json()["error_code"] == "route_not_found"


async def test_connect_failure_is_502() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    app, upstream = _gateway_with(httpx.MockTransport(boom))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        r = await c.get("/v1/search")
    await upstream.aclose()
    assert r.status_code == 502
    assert r.json()["error_code"] == "upstream_unavailable"


async def test_timeout_is_504() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    app, upstream = _gateway_with(httpx.MockTransport(slow))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        r = await c.get("/v1/search")
    await upstream.aclose()
    assert r.status_code == 504
    assert r.json()["error_code"] == "upstream_timeout"
