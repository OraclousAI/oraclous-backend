"""Unit: the GraphMembershipClient — the KGS graph-side visibility check for ADR-029 bindings.

It targets the SIBLING knowledge-graph-service's /internal/v1/graphs; the outbound HTTP is served by
an httpx MockTransport (no live KGS). The decisive checks (mirroring the connector tests): the
caller's org/principal is FORWARDED (never the body) gated by the internal key so KGS scopes the
listing to the caller's tenant; the response is parsed into a {graph_id: name} map; and any
transport error / non-200 / malformed body is a fail-closed GraphMembershipError (no upstream-body
leak).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.services.graph_membership_client import (
    GraphMembershipClient,
    GraphMembershipError,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000005c5")
_GRAPH = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _gateway_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Default to gateway mode so the org-identity forwarding is exercised."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    monkeypatch.setenv("AUTH_MODE", "gateway")
    monkeypatch.setenv("KNOWLEDGE_GRAPH_URL", "http://knowledge-graph-service:8000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> GraphMembershipClient:
    return GraphMembershipClient(transport=httpx.MockTransport(handler))


async def test_forwards_principal_and_returns_id_name_map() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        assert request.url.path == "/internal/v1/graphs"
        return httpx.Response(
            200,
            json={
                "graphs": [
                    {"id": str(_GRAPH), "name": "Acme support KB"},
                    {"id": "22222222-2222-2222-2222-222222222222", "name": "Acme sales KB"},
                ]
            },
        )

    out = await _client(handler).accessible_graphs(organisation_id=_ORG, user_id=_USER)

    # the caller's org/principal are FORWARDED, gated by the internal key (ADR-018) — never a body.
    assert captured["x-organisation-id"] == str(_ORG)
    assert captured["x-principal-id"] == str(_USER)
    assert captured["x-internal-key"] == "dev-internal-key"
    # parsed into a {graph_id: name} map.
    assert out[_GRAPH] == "Acme support KB"
    assert len(out) == 2


async def test_dev_mode_forwards_bearer_not_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DEV_BEARER", "dev-token")
    get_settings.cache_clear()
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"graphs": []})

    await _client(handler).accessible_graphs(organisation_id=_ORG, user_id=_USER)
    assert captured["authorization"] == "Bearer dev-token"
    assert "x-internal-key" not in captured  # dev path forwards the bearer, not the internal key


async def test_non_200_is_failclosed_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "kgs boom — secret internal detail"})

    with pytest.raises(GraphMembershipError) as exc:
        await _client(handler).accessible_graphs(organisation_id=_ORG, user_id=_USER)
    # the coarse status may appear; the upstream body must NOT (no-leak).
    assert "secret internal detail" not in str(exc.value)


async def test_malformed_body_is_failclosed_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not_graphs": []})

    with pytest.raises(GraphMembershipError):
        await _client(handler).accessible_graphs(organisation_id=_ORG, user_id=_USER)


async def test_transport_error_is_failclosed_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kgs unreachable")

    with pytest.raises(GraphMembershipError):
        await _client(handler).accessible_graphs(organisation_id=_ORG, user_id=_USER)
