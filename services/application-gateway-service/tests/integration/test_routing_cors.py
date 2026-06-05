"""Integration: all five upstreams route correctly + CORS + /internal not edge-routed (GW-4).

The mock upstream echoes the ``Host`` header the gateway set (= the resolved upstream), so each of
the five prefixes is verified to route to the RIGHT upstream. CORS preflight is answered at the
edge; the platform-internal ``/internal/*`` plane is not edge-routed (gateway 404, never forwarded).
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


async def _echo(request):  # noqa: ANN001 — echoes the Host the gateway addressed (= the upstream)
    return JSONResponse({"host": request.headers.get("host"), "path": request.url.path})


# a single mock upstream; ASGITransport dispatches by path, and the echoed Host proves which
# upstream base URL the gateway built the request for.
_UPSTREAM_APP = Starlette(routes=[Route("/{p:path}", _echo, methods=["GET", "OPTIONS"])])


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from oraclous_application_gateway_service.app.factory import create_app
    from oraclous_application_gateway_service.core.config import get_settings
    from oraclous_application_gateway_service.domain.route_table import build_route_table
    from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
    from oraclous_application_gateway_service.services.proxy_service import ProxyService

    get_settings.cache_clear()
    app = create_app(lifespan=None)
    upstream = httpx.AsyncClient(transport=ASGITransport(app=_UPSTREAM_APP))
    app.state.http_client = upstream
    table = build_route_table(get_settings())
    app.state.route_table = table
    app.state.proxy_service = ProxyService(
        route_table=table, upstream_client=UpstreamClient(upstream)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        yield c
    await upstream.aclose()


def _auth() -> dict:
    return {"Authorization": "Bearer dev-token"}


@pytest.mark.parametrize(
    ("path", "expected_host", "needs_auth"),
    [
        ("/v1/auth/me", "auth-service:8000", False),  # public allow-list
        ("/oauth/google/callback", "auth-service:8000", False),  # public
        ("/credentials/providers", "credential-broker-service:8000", True),
        ("/api/v1/graphs/g1", "knowledge-graph-service:8000", True),
        ("/api/v1/recipes", "knowledge-graph-service:8000", True),
        ("/v1/search", "knowledge-retriever-service:8000", True),
        ("/api/v1/tools", "capability-registry-service:8000", True),
        ("/api/v1/instances", "capability-registry-service:8000", True),
    ],
)
async def test_every_prefix_routes_to_the_right_upstream(
    client: AsyncClient, path: str, expected_host: str, needs_auth: bool
) -> None:
    r = await client.get(path, headers=_auth() if needs_auth else {})
    assert r.status_code == 200, r.text
    assert r.json()["host"] == expected_host  # routed to the correct upstream base URL


async def test_cors_preflight_is_answered_at_the_edge(client: AsyncClient) -> None:
    r = await client.options(
        "/api/v1/tools",
        headers={
            "Origin": "https://app.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") in ("https://app.test", "*")


async def test_cors_header_on_proxied_response(client: AsyncClient) -> None:
    r = await client.get("/api/v1/tools", headers={**_auth(), "Origin": "https://app.test"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") in ("https://app.test", "*")


async def test_internal_plane_is_not_edge_routed(client: AsyncClient) -> None:
    # /internal/* has no route entry — even authenticated, it is a gateway 404 (never forwarded)
    r = await client.get("/internal/agent-credentials", headers=_auth())
    assert r.status_code == 404
    assert r.json()["error_code"] == "route_not_found"
