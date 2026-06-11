"""Unit: the GraphIngestConnector — the write twin of the knowledge-retriever connector.

It targets the SIBLING knowledge-graph-service's /internal/v1/ingest; the outbound HTTP is served by
an httpx MockTransport (no live KGS). The decisive checks (mirroring the retriever connector's
tests): the caller's org/principal is FORWARDED (never the body) so the KGS's own scoping binds the
ingest to the caller's tenant + the internal key gates it; a missing graph_id/content never reaches
the network; an upstream 4xx is a clean structured failure that never echoes the upstream body
(no-leak); and the connector is registered as a builtin with an executor.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.graph_ingest import GraphIngestConnector
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import (
    _EXECUTORS,
    create_executor,
    has_executor,
)
from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.domain.plugins.builtin import GraphIngestPlugin

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


def _connector(handler: Callable[[httpx.Request], httpx.Response]) -> GraphIngestConnector:
    ex = GraphIngestConnector({"id": "x"})
    ex.transport = httpx.MockTransport(handler)
    return ex


def _job(graph_id: str = _GRAPH) -> dict:
    return {
        "id": "job-1",
        "graph_id": graph_id,
        "source_type": "text",
        "status": "pending",
    }


async def test_ingest_forwards_org_and_returns_job() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["org"] = req.headers.get("X-Organisation-Id")
        seen["principal"] = req.headers.get("X-Principal-Id")
        seen["principal_type"] = req.headers.get("X-Principal-Type")
        seen["internal_key"] = req.headers.get("X-Internal-Key")
        seen["body"] = json.loads(req.content)
        return httpx.Response(202, json=_job())

    ex = _connector(handler)
    res = await ex.execute({"graph_id": _GRAPH, "content": "hello"}, _ctx())
    assert res.success
    assert res.data == {"job_id": "job-1", "status": "pending"}
    assert seen["path"] == "/internal/v1/ingest"
    # the caller's org is forwarded so the KGS scopes the ingest to the SAME tenant (never the body)
    assert seen["org"] == str(_ORG)
    assert seen["principal"] == str(_USER)
    assert seen["principal_type"] == "agent"
    assert seen["internal_key"] == "dev-internal-key"
    assert seen["body"] == {"graph_id": _GRAPH, "content": "hello"}
    assert "organisation_id" not in seen["body"]  # the org is forwarded as a header, never the body


async def test_source_type_and_recipe_are_forwarded() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(202, json=_job())

    ex = _connector(handler)
    await ex.execute(
        {"graph_id": _GRAPH, "content": "[]", "source_type": "json", "recipe_id": "rcp_x"}, _ctx()
    )
    assert seen["body"]["source_type"] == "json"
    assert seen["body"]["recipe_id"] == "rcp_x"


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
        return httpx.Response(202, json=_job())

    ex = _connector(handler)
    res = await ex.execute({"graph_id": _GRAPH, "content": "hi"}, _ctx())
    assert res.success
    assert seen["auth"] == "Bearer dev-token"
    assert seen["org"] is None  # dev mode forwards a bearer; the KGS maps it to the dev org


async def test_missing_graph_id_never_reaches_the_network() -> None:
    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(202, json=_job())

    ex = _connector(handler)
    res = await ex.execute({"content": "hi"}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"
    assert called["n"] == 0


async def test_missing_content_is_rejected() -> None:
    ex = _connector(lambda _r: httpx.Response(202, json=_job()))
    res = await ex.execute({"graph_id": _GRAPH}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_upstream_4xx_is_a_clean_failure_that_does_not_leak_the_body() -> None:
    ex = _connector(
        lambda _r: httpx.Response(404, json={"detail": "graph 11111111 not found for org SECRET"})
    )
    res = await ex.execute({"graph_id": _GRAPH, "content": "hi"}, _ctx())
    assert not res.success and res.error_type == "INGEST_HTTP_ERROR"
    assert res.metadata.get("status_code") == 404
    assert "SECRET" not in (res.error_message or "")  # the upstream body is never echoed


async def test_malformed_body_is_a_clean_bad_response() -> None:
    ex = _connector(lambda _r: httpx.Response(202, json={"no": "id"}))
    res = await ex.execute({"graph_id": _GRAPH, "content": "hi"}, _ctx())
    assert not res.success and res.error_type == "INGEST_BAD_RESPONSE"


async def test_a_transport_error_is_unreachable() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    ex = _connector(handler)
    res = await ex.execute({"graph_id": _GRAPH, "content": "hi"}, _ctx())
    assert not res.success and res.error_type == "INGEST_UNREACHABLE"


# --- builtin registration -----------------------------------------------------
def test_graph_ingest_is_registered_as_a_builtin_with_an_executor() -> None:
    gid = GraphIngestPlugin.plugin_id()
    assert gid in {p.plugin_id() for p in plugin_registry.discover()}
    desc = GraphIngestPlugin.descriptor()
    assert desc["metadata"]["name"] == "Graph Ingest"
    assert desc["spec"]["credential_requirements"] == []  # credential-less (internal trust path)
    assert desc["spec"]["input_schema"]["required"] == ["graph_id", "content"]
    # the descriptor maps to the GraphIngestConnector executor
    assert has_executor(desc)
    assert gid in _EXECUTORS
    assert isinstance(create_executor(desc), GraphIngestConnector)
