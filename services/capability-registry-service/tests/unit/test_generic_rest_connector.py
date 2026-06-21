"""Unit: the GenericRestConnector — curated REST sources over a SSRF-guarded GET (#489).

Decisive checks: each curated source/endpoint dispatches and its response parses to a dict; an
unknown source / endpoint is rejected; the shared egress gate is consulted (an unsafe URL never
hits the network); upstream HTTP/transport/parse failures are coarse, body-free, fail-closed. The
outbound HTTP is served by a MockTransport and egress_allowed is patched so the happy paths need
no real DNS; the SSRF path drives egress_allowed to False and asserts no request is made.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from oraclous_capability_registry_service.domain.connectors import generic_rest
from oraclous_capability_registry_service.domain.connectors.generic_rest import GenericRestConnector
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit

_MODULE = "oraclous_capability_registry_service.domain.connectors.generic_rest.egress_allowed"


@pytest.fixture
def _allow_egress(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def _ok(_url: str) -> bool:
        return True

    monkeypatch.setattr(_MODULE, _ok)
    yield


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
    )


def _connector(handler: Callable[[httpx.Request], httpx.Response]) -> GenericRestConnector:
    ex = GenericRestConnector({"id": "x"})
    ex.transport = httpx.MockTransport(handler)
    return ex


@pytest.mark.usefixtures("_allow_egress")
async def test_mempool_tip_height_parses_to_an_int() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, text="850123")

    res = await _connector(handler).execute(
        {"source_id": "mempool", "endpoint": "tip_height"}, _ctx()
    )
    assert res.success and res.data == {"block_height": 850123}
    assert res.metadata == {"source_id": "mempool", "endpoint": "tip_height"}
    assert seen["url"] == "https://mempool.space/api/blocks/tip/height"


@pytest.mark.usefixtures("_allow_egress")
async def test_alternative_me_fear_greed_parses() -> None:
    body = {"data": [{"value": "45", "value_classification": "Fear"}]}
    res = await _connector(lambda _r: httpx.Response(200, json=body)).execute(
        {"source_id": "alternative_me", "endpoint": "fear_greed"}, _ctx()
    )
    assert res.success and res.data == {"value": 45, "classification": "Fear"}


async def test_unknown_source_fails_closed() -> None:
    res = await _connector(lambda _r: httpx.Response(200, text="x")).execute(
        {"source_id": "definitely-not-a-source", "endpoint": "x"}, _ctx()
    )
    assert not res.success and res.error_type == "UNKNOWN_SOURCE"


async def test_unknown_endpoint_is_rejected() -> None:
    res = await _connector(lambda _r: httpx.Response(200, text="x")).execute(
        {"source_id": "mempool", "endpoint": "nope"}, _ctx()
    )
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_missing_source_id_is_rejected() -> None:
    res = await _connector(lambda _r: httpx.Response(200, text="x")).execute(
        {"endpoint": "tip_height"}, _ctx()
    )
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_unsafe_url_is_refused_before_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _deny(_url: str) -> bool:
        return False

    monkeypatch.setattr(_MODULE, _deny)
    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, text="should-not-happen")

    res = await _connector(handler).execute(
        {"source_id": "mempool", "endpoint": "tip_height"}, _ctx()
    )
    assert not res.success and res.error_type == "UNSAFE_URL"
    assert called["n"] == 0


@pytest.mark.usefixtures("_allow_egress")
async def test_upstream_non_200_is_a_clean_failure() -> None:
    res = await _connector(lambda _r: httpx.Response(503, text="down")).execute(
        {"source_id": "mempool", "endpoint": "tip_height"}, _ctx()
    )
    assert not res.success and res.error_type == "SOURCE_HTTP_ERROR"
    assert res.metadata.get("status_code") == 503


@pytest.mark.usefixtures("_allow_egress")
async def test_a_transport_error_is_unreachable() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    res = await _connector(handler).execute(
        {"source_id": "mempool", "endpoint": "tip_height"}, _ctx()
    )
    assert not res.success and res.error_type == "FETCH_UNREACHABLE"


@pytest.mark.usefixtures("_allow_egress")
async def test_a_bad_response_shape_is_clean() -> None:
    res = await _connector(lambda _r: httpx.Response(200, text="not-an-int")).execute(
        {"source_id": "mempool", "endpoint": "tip_height"}, _ctx()
    )
    assert not res.success and res.error_type == "SOURCE_BAD_RESPONSE"


def test_plugin_is_registered_and_factory_resolves_it() -> None:
    from oraclous_capability_registry_service.domain.executors.factory import create_executor
    from oraclous_capability_registry_service.domain.plugins import plugin_registry
    from oraclous_capability_registry_service.domain.plugins.builtin import RestConnectorPlugin

    assert RestConnectorPlugin in set(plugin_registry.discover())
    assert isinstance(create_executor(RestConnectorPlugin.descriptor()), GenericRestConnector)
    assert generic_rest is not None  # module import sanity
