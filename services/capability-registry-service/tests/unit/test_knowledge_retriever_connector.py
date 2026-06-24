"""Unit: the KnowledgeRetrieverConnector — endpoint by mode, org-identity forwarding, no-leak.

The connector targets the SIBLING knowledge-retriever; the outbound HTTP is served by an httpx
MockTransport (no live retriever). The decisive checks: the caller's org is forwarded so the
retriever's own scoping binds the search (a caller can never read another org's graph); the ``mode``
param selects the endpoint; a missing graph_id never reaches the network; an upstream 4xx is a clean
structured failure that never echoes the upstream body.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.knowledge_retriever import (
    KnowledgeRetrieverConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000005c5")
_GRAPH = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _gateway_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Default the connector to gateway mode so the org-identity forwarding is exercised."""
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


def _connector(handler: Callable[[httpx.Request], httpx.Response]) -> KnowledgeRetrieverConnector:
    ex = KnowledgeRetrieverConnector({"id": "x"})
    ex.transport = httpx.MockTransport(handler)
    return ex


async def test_semantic_search_forwards_org_and_returns_hits() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["org"] = req.headers.get("X-Organisation-Id")
        seen["principal"] = req.headers.get("X-Principal-Id")
        seen["internal_key"] = req.headers.get("X-Internal-Key")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=[{"id": "n1", "type": "Chunk", "properties": {"t": "hi"}}])

    ex = _connector(handler)
    res = await ex.execute({"graph_id": _GRAPH, "query": "what is x"}, _ctx())
    assert res.success
    assert res.data == {"hits": [{"id": "n1", "type": "Chunk", "properties": {"t": "hi"}}]}
    assert res.metadata == {"mode": "semantic", "hit_count": 1}
    # default mode → /v1/search/semantic
    assert seen["path"] == "/v1/search/semantic"
    # the caller's org is forwarded so the retriever scopes the search to the SAME tenant
    assert seen["org"] == str(_ORG)
    assert seen["principal"] == str(_USER)
    assert seen["internal_key"] == "dev-internal-key"
    assert seen["body"] == {"query": "what is x", "graph_id": _GRAPH, "top_k": 10}


@pytest.mark.parametrize("mode", ["semantic", "fulltext", "hybrid"])
async def test_mode_selects_the_endpoint(mode: str) -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, json=[])

    ex = _connector(handler)
    res = await ex.execute({"graph_id": _GRAPH, "query": "q", "mode": mode, "top_k": 3}, _ctx())
    assert res.success and seen["path"] == f"/v1/search/{mode}"


async def test_top_k_is_forwarded() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=[])

    ex = _connector(handler)
    await ex.execute({"graph_id": _GRAPH, "query": "q", "top_k": 25}, _ctx())
    assert seen["body"]["top_k"] == 25


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
    res = await ex.execute({"graph_id": _GRAPH, "query": "q"}, _ctx())
    assert res.success
    assert seen["auth"] == "Bearer dev-token"
    assert seen["org"] is None  # dev mode forwards a bearer; the retriever maps it to the dev org


async def test_missing_graph_id_never_reaches_the_network() -> None:
    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json=[])

    ex = _connector(handler)
    res = await ex.execute({"query": "q"}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"
    assert called["n"] == 0


async def test_missing_query_is_rejected() -> None:
    ex = _connector(lambda _r: httpx.Response(200, json=[]))
    res = await ex.execute({"graph_id": _GRAPH}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_invalid_mode_is_rejected() -> None:
    ex = _connector(lambda _r: httpx.Response(200, json=[]))
    res = await ex.execute({"graph_id": _GRAPH, "query": "q", "mode": "fuzzy"}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_upstream_4xx_is_a_clean_failure_that_does_not_leak_the_body() -> None:
    ex = _connector(
        lambda _r: httpx.Response(404, json={"detail": "graph 11111111 not found for org SECRET"})
    )
    res = await ex.execute({"graph_id": _GRAPH, "query": "q"}, _ctx())
    assert not res.success and res.error_type == "RETRIEVER_HTTP_ERROR"
    assert res.metadata.get("status_code") == 404
    assert "SECRET" not in (res.error_message or "")  # the upstream body is never echoed


async def test_a_non_list_body_is_a_clean_bad_response() -> None:
    ex = _connector(lambda _r: httpx.Response(200, json={"not": "a list"}))
    res = await ex.execute({"graph_id": _GRAPH, "query": "q"}, _ctx())
    assert not res.success and res.error_type == "RETRIEVER_BAD_RESPONSE"


async def test_a_transport_error_is_unreachable() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    ex = _connector(handler)
    res = await ex.execute({"graph_id": _GRAPH, "query": "q"}, _ctx())
    assert not res.success and res.error_type == "RETRIEVER_UNREACHABLE"


def _ctx_with(configuration: dict) -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        execution_id=uuid.uuid4(),
        configuration=configuration,
    )


async def test_bound_precedence_is_forwarded_to_the_retriever_search() -> None:
    """#538: the run binds the team's precedence on the instance config → the connector forwards it
    to /v1/search so the retriever ranks canonical-first (the model never supplies it)."""
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=[{"id": "n1", "type": "Chunk", "properties": {}}])

    ex = _connector(handler)
    prec = {"order": ["rules", "bible", "drafts"], "graph_authoritative": True}
    res = await ex.execute(
        {"query": "x", "mode": "hybrid"},
        _ctx_with({"graph_id": _GRAPH, "precedence": prec}),
    )
    assert res.success
    assert seen["body"]["precedence"] == prec  # forwarded verbatim from the bound config


async def test_unbound_precedence_is_omitted_from_the_search() -> None:
    """Additive (#536/#538): no bound precedence → the search body is unchanged."""
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=[])

    ex = _connector(handler)
    res = await ex.execute({"query": "x"}, _ctx_with({"graph_id": _GRAPH}))
    assert res.success
    assert "precedence" not in seen["body"]
