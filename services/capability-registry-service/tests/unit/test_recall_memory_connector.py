"""Unit: the RecallMemoryConnector (#332 / ADR-027 §6) — endpoint, org forwarding, no-leak.

Mirrors the find-similar connector test: the connector targets the knowledge-graph-service's
``/api/v1/graphs/{graph_id}/memories/search``; the outbound HTTP is served by an httpx
MockTransport (no live KGS). The decisive checks: the caller's org is forwarded so the KGS's own
scoping binds the recall; query/type/scope/limit forward as query params; a missing
graph_id/query or an invalid type/scope never reaches the network; an upstream 4xx is a clean
structured failure that never echoes the upstream body; and the connector is registered as a
builtin (slug ``recall-memory``) with an executor.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.recall_memory import (
    RecallMemoryConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import (
    create_executor,
    has_executor,
)
from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.domain.plugins.builtin import RecallMemoryPlugin

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000005c5")
_GRAPH = "11111111-1111-1111-1111-111111111111"

_MEMORY = {
    "memory_id": "m-1",
    "type": "semantic",
    "content": "User prefers dark mode",
    "importance_score": 0.8,
    "relevance_score": 0.7,
    "confidence": 0.9,
    "scope": "agent",
}


@pytest.fixture(autouse=True)
def _gateway_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    monkeypatch.setenv("AUTH_MODE", "gateway")
    monkeypatch.setenv("KNOWLEDGE_GRAPH_URL", "http://knowledge-graph-service:8000")
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


def _connector(handler: Callable[[httpx.Request], httpx.Response]) -> RecallMemoryConnector:
    ex = RecallMemoryConnector({"id": "x"})
    ex.transport = httpx.MockTransport(handler)
    return ex


async def test_recall_forwards_org_path_and_params() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        seen["org"] = req.headers.get("X-Organisation-Id")
        seen["principal"] = req.headers.get("X-Principal-Id")
        seen["internal_key"] = req.headers.get("X-Internal-Key")
        return httpx.Response(200, json={"memories": [_MEMORY], "total": 1})

    ex = _connector(handler)
    res = await ex.execute(
        {
            "graph_id": _GRAPH,
            "query": "dark mode",
            "type": "semantic",
            "scope": "agent",
            "limit": 5,
        },
        _ctx(),
    )
    assert res.success
    assert res.data == {"memories": [_MEMORY], "total": 1}
    assert res.metadata == {"memory_count": 1}
    assert seen["path"] == f"/api/v1/graphs/{_GRAPH}/memories/search"
    assert seen["params"] == {
        "query": "dark mode",
        "limit": "5",
        "type": "semantic",
        "scope": "agent",
    }
    # the caller's org binds the KGS's scoping → no cross-tenant memory read.
    assert seen["org"] == str(_ORG)
    assert seen["principal"] == str(_USER)
    assert seen["internal_key"] == "dev-internal-key"


async def test_defaults_limit_and_omits_optional_filters() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"memories": [], "total": 0})

    res = await _connector(handler).execute({"graph_id": _GRAPH, "query": "q"}, _ctx())
    assert res.success
    assert seen["params"] == {"query": "q", "limit": "10"}


@pytest.mark.parametrize(
    "input_data",
    [
        {"query": "q"},  # missing graph_id
        {"graph_id": _GRAPH},  # missing query
        {"graph_id": " ", "query": "q"},  # blank graph_id
        {"graph_id": _GRAPH, "query": "q", "type": "nonsense"},  # invalid type
        {"graph_id": _GRAPH, "query": "q", "scope": "galaxy"},  # invalid scope
    ],
)
async def test_invalid_input_never_reaches_the_network(input_data: dict) -> None:
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover — must not be hit
        raise AssertionError("invalid input must fail closed before the network")

    res = await _connector(handler).execute(input_data, _ctx())
    assert not res.success
    assert res.error_type == "INVALID_INPUT"


async def test_upstream_error_is_structured_and_never_echoed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "graph not found", "secret": "tenant-data"})

    res = await _connector(handler).execute({"graph_id": _GRAPH, "query": "q"}, _ctx())
    assert not res.success
    assert res.error_type == "KGS_HTTP_ERROR"
    assert res.metadata == {"status_code": 404}
    assert "tenant-data" not in (res.error_message or "")  # the upstream body is never echoed


async def test_unreachable_kgs_is_a_clean_failure() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    res = await _connector(handler).execute({"graph_id": _GRAPH, "query": "q"}, _ctx())
    assert not res.success
    assert res.error_type == "KGS_UNREACHABLE"


async def test_malformed_body_is_a_clean_failure() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    res = await _connector(handler).execute({"graph_id": _GRAPH, "query": "q"}, _ctx())
    assert not res.success
    assert res.error_type == "KGS_BAD_RESPONSE"


def test_registered_as_builtin_with_executor() -> None:
    ids = {p.plugin_id() for p in plugin_registry.discover()}
    assert RecallMemoryPlugin.plugin_id() in ids
    descriptor = RecallMemoryPlugin.descriptor()
    # the ref's name slug contract: ``core/recall-memory@1.0.0`` → slug ``recall-memory``.
    assert descriptor["metadata"]["name"] == "Recall Memory"
    assert descriptor["spec"]["credential_requirements"] == []  # first-party, no credential
    assert has_executor(descriptor)
    assert isinstance(create_executor(descriptor), RecallMemoryConnector)
