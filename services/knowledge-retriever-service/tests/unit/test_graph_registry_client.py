"""Unit: GraphRegistryClient (#330) — the accessible-set enumeration seam (ADR-018 internal plane).

httpx MockTransport, no live KGS. Decisive: gateway mode forwards the caller's verified principal
+ org gated by the shared internal key (so KGS binds the enumeration to the SAME tenant); dev mode
forwards the fixed bearer; any failure (network, non-200, malformed body) raises — fail-closed,
never an empty/"all" fallback the caller could mistake for an answer.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_knowledge_retriever_service.services.graph_registry_client import (
    GraphInfo,
    GraphRegistryClient,
    GraphRegistryError,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000005c5")


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _client(handler, *, auth_mode: str = "gateway") -> GraphRegistryClient:
    return GraphRegistryClient(
        base_url="http://knowledge-graph-service:8000",
        auth_mode=auth_mode,
        dev_bearer="dev-token",
        internal_service_key="dev-internal-key",
        transport=httpx.MockTransport(handler),
    )


async def test_gateway_mode_forwards_principal_org_and_internal_key() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["org"] = req.headers.get("X-Organisation-Id")
        seen["principal"] = req.headers.get("X-Principal-Id")
        seen["key"] = req.headers.get("X-Internal-Key")
        return httpx.Response(
            200, json={"graphs": [{"id": str(uuid.UUID(int=1)), "name": "research"}]}
        )

    graphs = await _client(handler).accessible_graphs(_principal())
    assert graphs == [GraphInfo(id=str(uuid.UUID(int=1)), name="research")]
    assert seen["path"] == "/internal/v1/graphs"
    assert seen["org"] == str(_ORG)
    assert seen["principal"] == str(_USER)
    assert seen["key"] == "dev-internal-key"


async def test_dev_mode_forwards_the_bearer() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization")
        seen["org"] = req.headers.get("X-Organisation-Id")
        return httpx.Response(200, json={"graphs": []})

    await _client(handler, auth_mode="dev").accessible_graphs(_principal())
    assert seen["auth"] == "Bearer dev-token"
    assert seen["org"] is None


async def test_non_200_raises_fail_closed() -> None:
    client = _client(lambda _r: httpx.Response(503, json={"detail": "down"}))
    with pytest.raises(GraphRegistryError):
        await client.accessible_graphs(_principal())


async def test_malformed_body_raises_fail_closed() -> None:
    client = _client(lambda _r: httpx.Response(200, json={"not": "graphs"}))
    with pytest.raises(GraphRegistryError):
        await client.accessible_graphs(_principal())


async def test_network_error_raises_fail_closed() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(GraphRegistryError):
        await _client(handler).accessible_graphs(_principal())
