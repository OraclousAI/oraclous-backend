"""#1151 — the sibling of `test_model_credential_rejected_proxy.py` (#1108): end-to-end proof
that the engine's typed 502 for an unusable model answer (an unparseable reader output, or a
non-SUCCEEDED terminal run other than a credential rejection) survives the gateway reverse-proxy
boundary as itself, mirroring `test_proxy.py`'s own #866
`test_model_not_connected_survives_the_boundary` proof.

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

_CODE = "MODEL_ANSWER_UNUSABLE"
_LEAKED_SECRET = "sk-or-v1-SECRETKEYSHOULDNOTLEAK000000000000"  # noqa: S105 — fake, never real
_CURATED_MESSAGE = (
    "The model did not return a usable answer. Try again, or choose a different model."
)


async def _answer_unusable(request):  # noqa: ANN001 — starlette handler
    # The engine's own leaky detail is exactly what rule 8 exists to stop; the proof is that the
    # code arrives at the browser and the rest of the body — including the sentinel secret — does
    # not.
    return JSONResponse(
        {
            "detail": {
                "error_code": _CODE,
                "type": "reader_output_unparseable",
                "msg": f"reader could not parse the model's output, key {_LEAKED_SECRET}",
            }
        },
        status_code=502,
    )


async def _answer_unusable_as_422(request):  # noqa: ANN001 — starlette handler
    # SAME body, wrong status: proves the gateway's code/status agreement check (a mislabelled
    # upstream cannot dress a 422 as this code's canonical 502), distinct from the allow-list
    # check above.
    return JSONResponse(
        {
            "detail": {
                "error_code": _CODE,
                "type": "reader_output_unparseable",
                "msg": f"reader could not parse the model's output, key {_LEAKED_SECRET}",
            }
        },
        status_code=422,
    )


async def _bare_502(request):  # noqa: ANN001 — starlette handler
    # No error_code at all: today's status-derived SERVICE_UNAVAILABLE envelope. This guard is
    # green already (status_to_code(502) == SERVICE_UNAVAILABLE, independent of the new code) —
    # it exists so a later change to the allow-list or the status map cannot silently repoint a
    # code-less 502 at the new curated message.
    return JSONResponse({"detail": "upstream exploded"}, status_code=502)


_UPSTREAM_APP = Starlette(
    routes=[
        Route("/v1/engine/intake/readback", _answer_unusable, methods=["POST"]),
        Route("/v1/engine/intake/readback-422", _answer_unusable_as_422, methods=["POST"]),
        Route("/v1/engine/intake/readback-bare", _bare_502, methods=["POST"]),
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


async def test_model_answer_unusable_survives_the_boundary(client: AsyncClient) -> None:
    # Without this, the founder's browser sees a bare SERVICE_UNAVAILABLE and cannot tell "the
    # model's answer could not be used" apart from any other upstream 502 — the whole reason
    # #1151 needs the code.
    r = await client.post("/v1/engine/intake/readback", json={"idea": "x"}, headers=_auth())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == _CODE


async def test_the_curated_message_survives_not_just_the_code(client: AsyncClient) -> None:
    """The whole point of a NEW code rather than reusing SERVICE_UNAVAILABLE: a specific, curated,
    gateway-authored message reaches the browser (#1151 ruling) — no parse trace, no reader
    internals."""
    r = await client.post("/v1/engine/intake/readback", json={"idea": "x"}, headers=_auth())
    body = r.json()["error"]
    assert body["code"] == _CODE
    assert body["message"] == _CURATED_MESSAGE


async def test_model_answer_unusable_carries_none_of_the_upstream_body(
    client: AsyncClient,
) -> None:
    r = await client.post("/v1/engine/intake/readback", json={"idea": "x"}, headers=_auth())
    assert not scan_forbidden(r.text)
    assert _LEAKED_SECRET not in r.text
    assert "details" not in r.json()["error"]


async def test_the_same_body_on_a_422_is_not_relayed_as_the_code(client: AsyncClient) -> None:
    # A code must agree with the status it arrived on; an upstream naming MODEL_ANSWER_UNUSABLE
    # (a 502 policy) on a 422 gets the ordinary status-derived envelope, not a free choice of what
    # the browser is told.
    r = await client.post("/v1/engine/intake/readback-422", json={"idea": "x"}, headers=_auth())
    assert r.status_code == 422
    assert r.json()["error"]["code"] != _CODE
    assert _LEAKED_SECRET not in r.text


async def test_a_bare_502_with_no_code_stays_service_unavailable(client: AsyncClient) -> None:
    """Green today (status_to_code(502) == SERVICE_UNAVAILABLE already, with no code present to
    relay) — kept here as a guard against a future change quietly widening the new code onto
    every unlabelled 502."""
    r = await client.post("/v1/engine/intake/readback-bare", json={"idea": "x"}, headers=_auth())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
