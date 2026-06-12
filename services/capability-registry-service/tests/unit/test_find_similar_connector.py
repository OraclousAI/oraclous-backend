"""Unit: the FindSimilarConnector (#310) — GET-by-node endpoint, org-identity forwarding, no-leak.

Mirrors the knowledge-retriever connector test: the connector targets the SIBLING
knowledge-retriever's ``/v1/graph/{graph_id}/similar/{node_id}``; the outbound HTTP is served by an
httpx MockTransport (no live retriever). The decisive checks: the caller's org is forwarded so the
retriever's own scoping binds the lookup; the node_id/graph_id are in the path; top_k/min_score are
forwarded as query params; a missing graph_id/node_id never reaches the network; an upstream 4xx is
a clean structured failure that never echoes the upstream body; and the connector is registered as a
builtin with an executor.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.find_similar import (
    FindSimilarConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import create_executor
from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.domain.plugins.builtin import FindSimilarPlugin

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000005c5")
_GRAPH = "11111111-1111-1111-1111-111111111111"
_NODE = "4:abc:7"


@pytest.fixture(autouse=True)
def _gateway_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    monkeypatch.setenv("AUTH_MODE", "gateway")
    monkeypatch.setenv("KNOWLEDGE_RETRIEVER_URL", "http://knowledge-retriever-service:8000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        execution_id=uuid.uuid4(),
    )


def _connector(handler: Callable[[httpx.Request], httpx.Response]) -> FindSimilarConnector:
    ex = FindSimilarConnector({"id": "x"})
    ex.transport = httpx.MockTransport(handler)
    return ex


async def test_find_similar_forwards_org_path_and_params() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        seen["org"] = req.headers.get("X-Organisation-Id")
        seen["principal"] = req.headers.get("X-Principal-Id")
        seen["internal_key"] = req.headers.get("X-Internal-Key")
        return httpx.Response(
            200, json=[{"id": "n2", "type": "Item", "properties": {"score": 0.91}}]
        )

    ex = _connector(handler)
    res = await ex.execute(
        {"graph_id": _GRAPH, "node_id": _NODE, "top_k": 5, "min_score": 0.7}, _ctx()
    )
    assert res.success
    assert res.data == {"hits": [{"id": "n2", "type": "Item", "properties": {"score": 0.91}}]}
    assert res.metadata == {"hit_count": 1}
    assert seen["path"] == f"/v1/graph/{_GRAPH}/similar/{_NODE}"
    assert seen["params"] == {"top_k": "5", "min_score": "0.7"}
    # the caller's org binds the retriever's scoping → no cross-tenant read.
    assert seen["org"] == str(_ORG)
    assert seen["principal"] == str(_USER)
    assert seen["internal_key"] == "dev-internal-key"


async def test_defaults_top_k_and_min_score() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=[])

    ex = _connector(handler)
    res = await ex.execute({"graph_id": _GRAPH, "node_id": _NODE}, _ctx())
    assert res.success
    assert seen["params"] == {"top_k": "10", "min_score": "0.0"}


async def test_dev_mode_forwards_a_bearer_not_principal_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DEV_BEARER", "dev-token")
    get_settings.cache_clear()
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization")
        seen["org"] = req.headers.get("X-Organisation-Id")
        return httpx.Response(200, json=[])

    ex = _connector(handler)
    res = await ex.execute({"graph_id": _GRAPH, "node_id": _NODE}, _ctx())
    assert res.success
    assert seen["auth"] == "Bearer dev-token"
    assert seen["org"] is None


async def test_missing_graph_id_never_reaches_the_network() -> None:
    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json=[])

    ex = _connector(handler)
    res = await ex.execute({"node_id": _NODE}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"
    assert called["n"] == 0


async def test_missing_node_id_is_rejected() -> None:
    ex = _connector(lambda _r: httpx.Response(200, json=[]))
    res = await ex.execute({"graph_id": _GRAPH}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_upstream_4xx_is_a_clean_failure_that_does_not_leak_the_body() -> None:
    ex = _connector(
        lambda _r: httpx.Response(404, json={"detail": "graph not found for org SECRET"})
    )
    res = await ex.execute({"graph_id": _GRAPH, "node_id": _NODE}, _ctx())
    assert not res.success and res.error_type == "RETRIEVER_HTTP_ERROR"
    assert res.metadata.get("status_code") == 404
    assert "SECRET" not in (res.error_message or "")


async def test_a_non_list_body_is_a_clean_bad_response() -> None:
    ex = _connector(lambda _r: httpx.Response(200, json={"not": "a list"}))
    res = await ex.execute({"graph_id": _GRAPH, "node_id": _NODE}, _ctx())
    assert not res.success and res.error_type == "RETRIEVER_BAD_RESPONSE"


async def test_a_transport_error_is_unreachable() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    ex = _connector(handler)
    res = await ex.execute({"graph_id": _GRAPH, "node_id": _NODE}, _ctx())
    assert not res.success and res.error_type == "RETRIEVER_UNREACHABLE"


# --- builtin registration -------------------------------------------------------------------------
def test_find_similar_is_registered_as_a_builtin_with_an_executor() -> None:
    fid = FindSimilarPlugin.plugin_id()
    assert fid in {p.plugin_id() for p in plugin_registry.discover()}
    descriptor = FindSimilarPlugin.descriptor()
    executor = create_executor(descriptor)
    assert isinstance(executor, FindSimilarConnector)
