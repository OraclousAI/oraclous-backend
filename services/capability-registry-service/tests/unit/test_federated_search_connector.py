"""Unit: the FederatedSearchConnector (#330 / ADR-026) — POST body, org-identity forwarding,
no-leak.

Mirrors the find-similar connector test: the connector targets the knowledge-retriever's
``POST /v1/federated/search``; the outbound HTTP is served by an httpx MockTransport. The decisive
checks: the caller's org is forwarded so the retriever's own accessible-set enumeration binds the
fan-out (federation grants NO new access in-loop); query/mode/graph_ids/caps land in the JSON
body; a missing query never reaches the network; an upstream 403 (inaccessible subset, the
fail-closed gate) is a clean structured failure that never echoes the upstream body; and the
connector is registered as a builtin with an executor under the ``federated-search`` slug.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.federated_search import (
    FederatedSearchConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import create_executor
from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.domain.plugins.builtin import FederatedSearchPlugin

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000005c5")
_HIT = {
    "id": "4:x:1",
    "type": "Chunk",
    "properties": {"score": 0.9},
    "source_graph_id": "11111111-1111-1111-1111-111111111111",
    "source_graph_name": "research",
}


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


def _connector(handler: Callable[[httpx.Request], httpx.Response]) -> FederatedSearchConnector:
    ex = FederatedSearchConnector({"id": "x"})
    ex.transport = httpx.MockTransport(handler)
    return ex


async def test_forwards_org_and_posts_the_federated_body() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        seen["org"] = req.headers.get("X-Organisation-Id")
        seen["principal"] = req.headers.get("X-Principal-Id")
        seen["internal_key"] = req.headers.get("X-Internal-Key")
        return httpx.Response(
            200,
            json={"results": [_HIT], "total": 1, "meta": {"mode": "hybrid"}},
        )

    ex = _connector(handler)
    res = await ex.execute(
        {
            "query": "ada lovelace",
            "mode": "hybrid",
            "graph_ids": ["11111111-1111-1111-1111-111111111111"],
            "per_graph_k": 5,
            "total_k": 20,
        },
        _ctx(),
    )
    assert res.success
    assert res.data == {"results": [_HIT], "meta": {"mode": "hybrid"}}
    assert res.metadata == {"result_count": 1}
    assert seen["path"] == "/v1/federated/search"
    assert seen["body"] == {
        "query": "ada lovelace",
        "mode": "hybrid",
        "graph_ids": ["11111111-1111-1111-1111-111111111111"],
        "per_graph_k": 5,
        "total_k": 20,
    }
    # the caller's org binds the retriever's accessible-set enumeration → no new access in-loop.
    assert seen["org"] == str(_ORG)
    assert seen["principal"] == str(_USER)
    assert seen["internal_key"] == "dev-internal-key"


async def test_forwards_the_real_principal_type_not_a_hardcoded_one() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["ptype"] = req.headers.get("X-Principal-Type")
        return httpx.Response(200, json={"results": [], "total": 0, "meta": {}})

    ex = _connector(handler)
    ctx = ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        execution_id=uuid.uuid4(),
        principal_type="user",  # a real user-principal execution, not the harness agent loop
    )
    res = await ex.execute({"query": "ada"}, ctx)
    assert res.success
    assert seen["ptype"] == "user"


async def test_defaults_mode_and_omits_unset_fields() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"results": [], "total": 0, "meta": {}})

    ex = _connector(handler)
    res = await ex.execute({"query": "ada"}, _ctx())
    assert res.success
    assert seen["body"] == {"query": "ada", "mode": "hybrid"}


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
        return httpx.Response(200, json={"results": [], "meta": {}})

    ex = _connector(handler)
    res = await ex.execute({"query": "ada"}, _ctx())
    assert res.success
    assert seen["auth"] == "Bearer dev-token"
    assert seen["org"] is None


async def test_missing_query_never_reaches_the_network() -> None:
    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"results": [], "meta": {}})

    ex = _connector(handler)
    res = await ex.execute({"mode": "entity"}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"
    assert called["n"] == 0


async def test_bad_mode_and_bad_graph_ids_are_rejected() -> None:
    ex = _connector(lambda _r: httpx.Response(200, json={"results": [], "meta": {}}))
    res = await ex.execute({"query": "x", "mode": "psychic"}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"
    res = await ex.execute({"query": "x", "graph_ids": "not-a-list"}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_upstream_403_is_a_clean_failure_that_does_not_leak_the_body() -> None:
    # the retriever's fail-closed subset gate (an inaccessible graph id) surfaces as the coarse
    # status only — never the upstream detail.
    ex = _connector(
        lambda _r: httpx.Response(403, json={"detail": "graph SECRET is not accessible"})
    )
    res = await ex.execute({"query": "ada"}, _ctx())
    assert not res.success and res.error_type == "RETRIEVER_HTTP_ERROR"
    assert res.metadata.get("status_code") == 403
    assert "SECRET" not in (res.error_message or "")


async def test_a_malformed_body_is_a_clean_bad_response() -> None:
    ex = _connector(lambda _r: httpx.Response(200, json={"not": "results"}))
    res = await ex.execute({"query": "ada"}, _ctx())
    assert not res.success and res.error_type == "RETRIEVER_BAD_RESPONSE"


async def test_a_transport_error_is_unreachable() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    ex = _connector(handler)
    res = await ex.execute({"query": "ada"}, _ctx())
    assert not res.success and res.error_type == "RETRIEVER_UNREACHABLE"


# --- builtin registration ----------------------------------------------------------------------
def test_federated_search_is_registered_as_a_builtin_with_an_executor() -> None:
    fid = FederatedSearchPlugin.plugin_id()
    assert fid in {p.plugin_id() for p in plugin_registry.discover()}
    descriptor = FederatedSearchPlugin.descriptor()
    executor = create_executor(descriptor)
    assert isinstance(executor, FederatedSearchConnector)
    # the ref slug agents bind is core/federated-search@1.0.0 — the name slug must match.
    assert FederatedSearchPlugin.NAME.lower().replace(" ", "-") == "federated-search"
    assert descriptor["spec"]["credential_requirements"] == []  # first-party: no broker credential
