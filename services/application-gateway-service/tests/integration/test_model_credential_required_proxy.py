"""#949 ruling Q4 (C5, gateway half) — end-to-end proof that KRS's typed 422 survives the gateway
reverse-proxy boundary as itself, mirroring `test_proxy.py`'s own #866
`test_model_not_connected_survives_the_boundary` proof.

Self-contained (does not touch the shared `_UPSTREAM_APP` in `test_proxy.py`) so this commit's
diff stays scoped to C5 and does not risk a merge collision with unrelated proxy work.
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


async def _no_credential(request):  # noqa: ANN001 — starlette handler
    # KRS's own leaky detail is exactly what rule 8 exists to stop; the proof is that the code
    # arrives at the browser and the rest of the body does not.
    return JSONResponse(
        {
            "detail": {
                "error_code": "MODEL_CREDENTIAL_REQUIRED",
                "type": "model_credential_required",
                "msg": "org db-1.internal 10.0.0.5 has no model credential configured",
            }
        },
        status_code=422,
    )


_UPSTREAM_APP = Starlette(
    routes=[Route("/v1/search/semantic", _no_credential, methods=["POST"])],
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


async def test_model_credential_required_survives_the_boundary(client: AsyncClient) -> None:
    # Without this, the console's search screen shows a bare VALIDATION_FAILED and cannot tell
    # "connect a model" apart from any other invalid field — the whole reason #949 needs the code.
    r = await client.post(
        "/v1/search/semantic", json={"query": "x", "graph_id": "g"}, headers=_auth()
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "MODEL_CREDENTIAL_REQUIRED"


async def test_the_curated_message_survives_not_just_the_code(client: AsyncClient) -> None:
    """The whole point of a NEW code rather than reusing VALIDATION_FAILED: a specific, curated
    message reaches the browser, not a generic fallback message. Asserts the CODE too — a
    fallback envelope (e.g. today's pre-[impl] MALFORMED_REQUEST) also carries a message that
    happens to differ from VALIDATION_FAILED's, which would let this test pass for the wrong
    reason if it checked the message alone."""
    r = await client.post(
        "/v1/search/semantic", json={"query": "x", "graph_id": "g"}, headers=_auth()
    )
    body = r.json()["error"]
    assert body["code"] == "MODEL_CREDENTIAL_REQUIRED"
    assert body["message"]
    assert body["message"] != "One or more fields are invalid."
    assert body["message"] != "The request body could not be parsed."


async def test_model_credential_required_carries_none_of_the_upstream_body(
    client: AsyncClient,
) -> None:
    r = await client.post(
        "/v1/search/semantic", json={"query": "x", "graph_id": "g"}, headers=_auth()
    )
    assert not scan_forbidden(r.text)
    assert "db-1.internal" not in r.text and "10.0.0.5" not in r.text
    assert "details" not in r.json()["error"]
