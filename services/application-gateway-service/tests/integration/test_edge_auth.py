"""Integration: edge JWT termination + identity forwarding + anti-spoof (GW-3).

Proves: a protected route without/with-invalid token → 401 (before any upstream call); a public
allow-list path (/v1/auth/*) proxies through without a token; the gateway injects the VERIFIED
identity and STRIPS any client-forged X-Principal-* (anti-spoof); a public path passes a client
X-Organisation-Id hint through but still strips X-Principal-*.
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

_DEV_USER = "00000000-0000-0000-0000-0000000000e6"
_DEV_ORG = "00000000-0000-0000-0000-00000000050a"


async def _echo(request):  # noqa: ANN001 — echoes the identity headers the upstream received
    return JSONResponse(
        {
            "x_principal": request.headers.get("x-principal-id"),
            "x_org": request.headers.get("x-organisation-id"),
        }
    )


_UPSTREAM_APP = Starlette(
    routes=[
        Route("/v1/search", _echo, methods=["GET"]),
        Route("/v1/auth/login", _echo, methods=["GET"]),
    ]
)


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
        route_table=table, upstream_client=UpstreamClient(upstream), internal_key="ik-test"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        yield c
    await upstream.aclose()


def _auth() -> dict:
    return {"Authorization": "Bearer dev-token"}


async def test_protected_route_without_token_is_401(client: AsyncClient) -> None:
    r = await client.get("/v1/search")
    assert r.status_code == 401


async def test_invalid_token_is_401(client: AsyncClient) -> None:
    r = await client.get("/v1/search", headers={"Authorization": "Bearer not-the-dev-token"})
    assert r.status_code == 401


async def test_public_auth_path_bypasses_edge_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/auth/login")  # no token — public allow-list
    assert r.status_code == 200
    assert r.json()["x_principal"] is None  # no identity injected on public paths


async def test_verified_identity_is_injected(client: AsyncClient) -> None:
    r = await client.get("/v1/search", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["x_principal"] == _DEV_USER
    assert body["x_org"] == _DEV_ORG


async def test_client_forged_principal_is_stripped(client: AsyncClient) -> None:
    headers = {**_auth(), "X-Principal-Id": "11111111-1111-1111-1111-111111111111"}
    r = await client.get("/v1/search", headers=headers)
    assert r.status_code == 200
    # the upstream sees the gateway's VERIFIED principal, never the client-forged one
    assert r.json()["x_principal"] == _DEV_USER


async def test_public_path_passes_org_hint_but_strips_principal(client: AsyncClient) -> None:
    headers = {
        "X-Organisation-Id": "99999999-9999-9999-9999-999999999999",
        "X-Principal-Id": "11111111-1111-1111-1111-111111111111",
    }
    r = await client.get("/v1/auth/login", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["x_org"] == "99999999-9999-9999-9999-999999999999"  # login multi-org hint passes
    assert body["x_principal"] is None  # forged principal stripped even on public paths
