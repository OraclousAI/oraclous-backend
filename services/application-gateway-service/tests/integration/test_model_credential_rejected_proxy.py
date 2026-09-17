"""#1108 — the sibling of `test_model_credential_required_proxy.py` (#949 Q4/C5): end-to-end proof
that the engine's typed 422 for a provider-rejected model key survives the gateway reverse-proxy
boundary as itself, mirroring `test_proxy.py`'s own #866 `test_model_not_connected_survives_the_
boundary` proof.

Self-contained (does not touch the shared `_UPSTREAM_APP` in `test_proxy.py`) so this commit's
diff stays scoped and does not risk a merge collision with unrelated proxy work.
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

_LEAKED_SECRET = "sk-or-v1-SECRETKEYSHOULDNOTLEAK000000000000"  # noqa: S105 — fake, never real


async def _key_rejected(request):  # noqa: ANN001 — starlette handler
    # The engine's own leaky detail is exactly what rule 8 exists to stop; the proof is that the
    # code arrives at the browser and the rest of the body — including the rejected key itself —
    # does not.
    return JSONResponse(
        {
            "detail": {
                "error_code": "MODEL_CREDENTIAL_REJECTED",
                "type": "model_credential_rejected",
                "msg": f"provider rejected the connected key {_LEAKED_SECRET}",
            }
        },
        status_code=422,
    )


async def _key_rejected_as_500(request):  # noqa: ANN001 — starlette handler
    # SAME body, wrong status: proves the gateway's code/status agreement check (a mislabelled
    # upstream cannot dress a 500 as a 422), distinct from the allow-list check above.
    return JSONResponse(
        {
            "detail": {
                "error_code": "MODEL_CREDENTIAL_REJECTED",
                "type": "model_credential_rejected",
                "msg": f"provider rejected the connected key {_LEAKED_SECRET}",
            }
        },
        status_code=500,
    )


_UPSTREAM_APP = Starlette(
    routes=[
        Route("/v1/engine/intake/readback", _key_rejected, methods=["POST"]),
        Route("/v1/engine/intake/readback-500", _key_rejected_as_500, methods=["POST"]),
    ],
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


async def test_model_credential_rejected_survives_the_boundary(client: AsyncClient) -> None:
    # Without this, the founder's browser sees a bare READBACK_FAILED (or a generic 422) and
    # cannot tell "your key is bad" apart from any other read-back failure — the whole reason
    # #1108 needs the code.
    r = await client.post("/v1/engine/intake/readback", json={"idea": "x"}, headers=_auth())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "MODEL_CREDENTIAL_REJECTED"


async def test_the_curated_message_survives_not_just_the_code(client: AsyncClient) -> None:
    """The whole point of a NEW code rather than reusing VALIDATION_FAILED: a specific, curated,
    gateway-authored message reaches the browser (#1108 ruling 4) — no key, no credential name."""
    r = await client.post("/v1/engine/intake/readback", json={"idea": "x"}, headers=_auth())
    body = r.json()["error"]
    assert body["code"] == "MODEL_CREDENTIAL_REJECTED"
    assert (
        body["message"]
        == "Your model provider refused the connected model key. Replace the key and try again."
    )


async def test_model_credential_rejected_carries_none_of_the_upstream_body(
    client: AsyncClient,
) -> None:
    r = await client.post("/v1/engine/intake/readback", json={"idea": "x"}, headers=_auth())
    assert not scan_forbidden(r.text)
    assert _LEAKED_SECRET not in r.text
    assert "details" not in r.json()["error"]


async def test_the_same_body_on_a_500_is_not_relayed_as_the_code(client: AsyncClient) -> None:
    # A code must agree with the status it arrived on; an upstream naming MODEL_CREDENTIAL_REJECTED
    # (a 422 policy) on a 500 gets the ordinary status-derived envelope, not a free choice of what
    # the browser is told.
    r = await client.post("/v1/engine/intake/readback-500", json={"idea": "x"}, headers=_auth())
    assert r.status_code == 500
    assert r.json()["error"]["code"] != "MODEL_CREDENTIAL_REJECTED"
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert _LEAKED_SECRET not in r.text
