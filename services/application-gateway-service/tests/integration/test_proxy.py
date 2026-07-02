"""Integration: the reverse-proxy forwards + fails closed, under edge auth (GW-2 + GW-3).

The gateway app runs via ASGITransport; its internal upstream client points at a real mock upstream
(ASGITransport, which streams) for forward/passthrough/identity proofs, and at an httpx Mock
transport that raises for connect/timeout proofs. Authenticated requests carry the dev bearer; the
echoes the trusted identity headers the gateway injected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from tools.contract.error_envelope import scan_forbidden

pytestmark = pytest.mark.integration

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"


async def _search(request):  # noqa: ANN001 — starlette handler; echoes injected identity
    return JSONResponse(
        {
            "results": ["r1"],
            "via": "krs",
            "echo_q": request.url.query,
            "x_org": request.headers.get("x-organisation-id"),
            "x_principal": request.headers.get("x-principal-id"),
        }
    )


async def _caps(request):  # noqa: ANN001 — distinct upstream status for the normalisation proof
    return JSONResponse({"detail": "upstream says no"}, status_code=403)


async def _leaky(request):  # noqa: ANN001 — a 500 whose body would leak internals if relayed
    return JSONResponse(
        {"detail": "NullPointerException at com.x.Y(Y.java:10) on db-1.internal 10.0.0.5"},
        status_code=500,
    )


# A large body that spans many read flushes — the #235 truncation case (a detached stream over the
# pooled connection lost the terminator past ~one flush). ~250 KB, subgraph-shaped.
_BIG = {"nodes": [{"id": i, "blob": "x" * 240} for i in range(1000)], "edges": []}


async def _big(request):  # noqa: ANN001 — a large (~250 KB) proxied success body
    return JSONResponse(_BIG)


async def _struct422(request):  # noqa: ANN001 — Pydantic-shaped 422; msg AND dict-key loc reflect values
    return JSONResponse(
        {
            "detail": [
                {
                    "loc": ["body", "email"],
                    "type": "value_error",
                    "msg": "value is not a valid email: alice@corp.internal",
                },
                # a user-controlled dict KEY reflected into loc — the #225 review's leak vector
                {
                    "loc": ["body", "meta", "secret@corp.internal"],
                    "type": "int_parsing",
                    "msg": "x",
                },
                {"loc": ["body", "host", "db-1.svc.cluster.local"], "type": "missing", "msg": "x"},
            ]
        },
        status_code=422,
    )


async def _string422(request):  # noqa: ANN001 — a free-string 422 (cannot be proven value-free)
    return JSONResponse({"detail": "boom on db-1.internal 10.0.0.5"}, status_code=422)


async def _method405(request):  # noqa: ANN001 — an upstream 405 (Allow + a leaky detail body)
    # mirrors KGS POST /documents 405: an Allow header + a detail body the gateway must NOT relay.
    return JSONResponse(
        {"detail": "POST not supported; use /upload on db-1.internal 10.0.0.5"},
        status_code=405,
        headers={"Allow": "GET"},
    )


_UPSTREAM_APP = Starlette(
    routes=[
        Route("/v1/search", _search, methods=["GET"]),
        Route("/api/v1/capabilities", _caps, methods=["GET"]),
        Route("/api/v1/tools/boom", _leaky, methods=["GET"]),
        Route("/v1/graph/big/subgraph", _big, methods=["GET"]),
        Route("/api/v1/tools/weak", _struct422, methods=["POST"]),
        Route("/api/v1/tools/stringerr", _string422, methods=["POST"]),
        Route("/api/v1/graphs/g/documents", _method405, methods=["POST"]),
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
        route_table=table, upstream_client=UpstreamClient(upstream), internal_key="ik-test"
    )
    return app, upstream


def _auth() -> dict:
    return {"Authorization": "Bearer dev-token"}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app, upstream = _gateway_with(ASGITransport(app=_UPSTREAM_APP))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        yield c
    await upstream.aclose()


async def test_authed_request_is_forwarded_with_injected_identity(client: AsyncClient) -> None:
    r = await client.get("/v1/search?q=hello", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["via"] == "krs"
    assert body["echo_q"] == "q=hello"  # query forwarded verbatim
    assert body["x_org"] == _DEV_ORG  # gateway injected the verified org downstream
    assert body["x_principal"]  # gateway injected the verified principal id


async def test_large_proxied_body_is_relayed_intact(client: AsyncClient) -> None:
    # #235: a large (multi-flush) success body must come back WHOLE, not a truncated chunked stream.
    r = await client.get("/v1/graph/big/subgraph", headers=_auth())
    assert r.status_code == 200
    body = r.json()  # parses fully — a truncated body would raise here
    assert len(body["nodes"]) == 1000
    # buffered → a correct Content-Length, not a terminator-less chunked transfer
    assert "content-length" in r.headers
    assert int(r.headers["content-length"]) == len(r.content)


async def test_422_structured_surfaces_leak_safe_details(client: AsyncClient) -> None:
    # #225: a Pydantic 422 → VALIDATION_FAILED with field + a machine-token issue, NEVER the
    # value-reflecting msg NOR a value reflected into a dict-key loc part.
    r = await client.post("/api/v1/tools/weak", headers=_auth())
    assert r.status_code == 422
    env = r.json()["error"]
    assert env["code"] == "VALIDATION_FAILED"
    assert any(d["field"] == "email" and d["issue"] == "VALUE_ERROR" for d in env["details"])
    # the reflected msg AND the dict-key values are neutralised — verbatim never appears
    assert "alice@corp.internal" not in r.text and "secret@corp.internal" not in r.text
    assert "not a valid email" not in r.text and "db-1.svc.cluster.local" not in r.text
    # the §3 forbidden-substring scanner finds nothing in the surfaced body (no email/DNS/IP leak)
    assert scan_forbidden(r.text) == []


async def test_405_surfaces_allow_and_a_contract_pointer_without_leaking_the_body(
    client: AsyncClient,
) -> None:
    # #579: a wrong-method guess (POST a GET-only resource) returns an ACTIONABLE 405 — the upstream
    # Allow header + a curated pointer to the published contract — but the upstream detail body
    # (which here carries an internal host/IP) is NEVER relayed (§3 rule 8).
    r = await client.post("/api/v1/graphs/g/documents", headers=_auth())
    assert r.status_code == 405
    env = r.json()["error"]
    assert env["code"] == "METHOD_NOT_ALLOWED"
    assert "/v1/openapi.json" in env["message"]  # points to the discoverability surface
    assert r.headers.get("allow") == "GET"  # the safe, standard method-list header is surfaced
    assert "db-1.internal" not in r.text and "/upload" not in r.text  # upstream body NOT relayed


async def test_422_string_detail_falls_back_to_canonical(client: AsyncClient) -> None:
    # a free-string 422 cannot be proven value-free → canonical detail-free envelope, no leak.
    r = await client.post("/api/v1/tools/stringerr", headers=_auth())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "MALFORMED_REQUEST"
    assert "10.0.0.5" not in r.text and "db-1.internal" not in r.text


async def test_upstream_error_is_normalised(client: AsyncClient) -> None:
    # the upstream's own 403 status is preserved, but its body is replaced by the canonical envelope
    r = await client.get("/api/v1/capabilities", headers=_auth())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
    assert "detail" not in r.json()  # the upstream's raw body is not relayed
    assert "upstream says no" not in r.text


async def test_upstream_error_body_is_not_leaked(client: AsyncClient) -> None:
    # an upstream 500 whose body carries a stack trace + internal host/IP must not leak through (§3)
    r = await client.get("/api/v1/tools/boom", headers=_auth())
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert scan_forbidden(r.text) == []
    assert "NullPointerException" not in r.text
    assert "10.0.0.5" not in r.text


async def test_health_is_not_proxied(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "application-gateway"


async def test_unknown_prefix_is_gateway_404(client: AsyncClient) -> None:
    r = await client.get("/totally/unknown", headers=_auth())
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


async def test_connect_failure_is_502() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    app, upstream = _gateway_with(httpx.MockTransport(boom))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        r = await c.get("/v1/search", headers=_auth())
    await upstream.aclose()
    assert r.status_code == 502  # Bad Gateway status preserved
    assert r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


async def test_timeout_is_504() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    app, upstream = _gateway_with(httpx.MockTransport(slow))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        r = await c.get("/v1/search", headers=_auth())
    await upstream.aclose()
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "GATEWAY_TIMEOUT"
