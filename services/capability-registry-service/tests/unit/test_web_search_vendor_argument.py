"""Unit (#946 T1): the search vendor is an operator setting, not a model-facing argument.

The ``search`` operation of ``core/web-research`` used to declare ``provider`` in the flat
``parameters`` hint map. That map can express only a type, so the argument reached a model as a
bare unexplained string named after the search VENDOR — while the thing a person actually wants to
control is which websites the results come from (#951). Models filled it with website names, every
call failed ``UNKNOWN_PROVIDER``, and the model retried the same value until the budget ran out.

So: drop it from the model-facing operation. Every other path is unchanged — ``WEB_SEARCH_PROVIDER``
stays the operator default, an internal caller that passes ``provider`` explicitly still selects
that vendor, and an unrecognised vendor still fails closed (never a silent fallback, CLAUDE.md
§3.5). The refusal now names what IS registered, in the spirit of #899's near-match hint.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.search_providers import (
    SearchProviderError,
    available_providers,
    get_search_provider,
)
from oraclous_capability_registry_service.domain.connectors.web_research import (
    WebResearchConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.plugins.builtin import (
    WebResearchPlugin,
    WebSearchToolPlugin,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000946")
_USER = uuid.UUID("00000000-0000-0000-0000-000000000947")


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        execution_id=uuid.uuid4(),
        credentials={"api_key": {"api_key": "tvly-secret"}},
    )


def _connector(handler: Callable[[httpx.Request], httpx.Response]) -> WebResearchConnector:
    ex = WebResearchConnector({"id": "x"})
    ex.transport = httpx.MockTransport(handler)
    return ex


def _search_operation(plugin: type) -> dict:
    return next(op for op in plugin.CAPABILITIES if op["name"] == "search")


# --- the model-facing operation no longer names the vendor --------------------------------------


def test_the_search_operation_offers_no_vendor_argument_to_a_model() -> None:
    assert "provider" not in _search_operation(WebResearchPlugin)["parameters"]


def test_the_standard_search_tool_offers_no_vendor_argument_either() -> None:
    assert "provider" not in _search_operation(WebSearchToolPlugin)["parameters"]


def test_the_operations_a_model_can_still_reach_are_unchanged() -> None:
    # dropping one argument must not drop an operation — search/fetch/read all survive
    assert {op["name"] for op in WebResearchPlugin.CAPABILITIES} == {"search", "fetch", "read"}


def test_the_connectors_own_input_schema_keeps_the_vendor_field() -> None:
    # the internal-caller path is unchanged: only the MODEL-facing hint map loses it
    assert "provider" in WebResearchPlugin.INPUT_SCHEMA["properties"]


# --- the internal-caller path still works ------------------------------------------------------


async def test_an_explicitly_passed_vendor_still_selects_that_vendor() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["host"] = req.url.host
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"results": []})

    ex = _connector(handler)
    res = await ex.execute(
        {"operation": "search", "query": "night trains", "provider": "tavily"}, _ctx()
    )
    assert res.success
    assert res.metadata["provider"] == "tavily"
    assert seen["host"] == "api.tavily.com"


@pytest.mark.security
async def test_an_unrecognised_vendor_still_fails_closed_and_never_searches() -> None:
    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"results": []})

    ex = _connector(handler)
    res = await ex.execute({"operation": "search", "query": "q", "provider": "The Verge"}, _ctx())
    assert not res.success
    assert res.error_type == "UNKNOWN_PROVIDER"
    assert called["n"] == 0  # no silent fallback to the default vendor


# --- the refusal names what is registered ------------------------------------------------------


def test_the_refusal_names_the_registered_vendors() -> None:
    with pytest.raises(SearchProviderError) as exc:
        get_search_provider("The Verge")
    message = str(exc.value)
    assert "The Verge" in message
    for name in available_providers():
        assert name in message


@pytest.mark.security
def test_the_refusal_is_still_typed_unknown_provider() -> None:
    with pytest.raises(SearchProviderError) as exc:
        get_search_provider("nope")
    assert exc.value.error_type == "UNKNOWN_PROVIDER"


async def test_the_refusal_reaches_the_caller_naming_the_registered_vendors() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("the provider factory must refuse before any request")

    ex = _connector(handler)
    res = await ex.execute({"operation": "search", "query": "q", "provider": "bbc.co.uk"}, _ctx())
    assert not res.success
    assert "tavily" in (res.error_message or "")


# --- the model-facing schema, not just the hint map behind it ------------------------------------
#
# The tests above assert on `CAPABILITIES[...]["parameters"]`, which is the descriptor's flat hint
# map. The criterion is about the SCHEMA a member is handed, one hop further on. The hop is short —
# `tool_schemas._parameters_for` is the map's only consumer repo-wide — but it is the hop that
# matters, and the two services cannot import each other, so the schema half is pinned by shape.


def test_the_schema_built_from_this_descriptor_declares_no_vendor_property() -> None:
    """Mirrors `harness-runtime`'s `_json_schema`: one property per hint-map key. Kept here rather
    than left implicit, so removing the key from the map is demonstrably enough to remove the
    property from what a model sees."""
    parameters = _search_operation(WebResearchPlugin)["parameters"]
    properties = {str(key): {"type": "string"} for key in parameters}
    assert "provider" not in properties
    assert {"query", "max_results"} <= set(properties)
