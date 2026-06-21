"""Unit: the SearchProvider factory + the Tavily provider.

The factory is the extension point (Reza's requirement): register a provider by name, build it by
name, fail-closed on an unknown name. Tavily is exercised against an httpx MockTransport (no live
API): the api_key + query travel in the body, the ``results[]`` normalize to SearchHits, and every
failure mode (non-200 / non-JSON / malformed / transport error) is a coarse, body-free
SearchProviderError that never echoes the upstream body.
"""

from __future__ import annotations

import json

import httpx
import pytest
from oraclous_capability_registry_service.domain.connectors.search_providers import (
    SearchProvider,
    SearchProviderError,
    TavilySearchProvider,
    available_providers,
    clamp_max_results,
    get_search_provider,
    register_search_provider,
)

pytestmark = pytest.mark.unit


def test_tavily_is_registered_in_the_factory() -> None:
    assert "tavily" in available_providers()
    assert isinstance(get_search_provider("tavily"), TavilySearchProvider)


def test_unknown_provider_fails_closed() -> None:
    with pytest.raises(SearchProviderError) as exc:
        get_search_provider("does-not-exist")
    assert exc.value.error_type == "UNKNOWN_PROVIDER"


def test_register_adds_a_new_provider_without_touching_the_connector() -> None:
    @register_search_provider
    class _StubProvider(SearchProvider):
        name = "stub-test-provider"

        async def search(  # type: ignore[no-untyped-def]
            self, query, *, api_key, max_results=5, transport=None
        ):
            return []

    try:
        assert "stub-test-provider" in available_providers()
        assert isinstance(get_search_provider("stub-test-provider"), _StubProvider)
    finally:
        from oraclous_capability_registry_service.domain.connectors import search_providers

        search_providers._PROVIDERS.pop("stub-test-provider", None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 1), (1, 1), (5, 5), (20, 20), (99, 20), (-3, 1), (None, 5), ("x", 5), (True, 5)],
)
def test_clamp_max_results(value: object, expected: int) -> None:
    assert clamp_max_results(value) == expected


async def test_tavily_sends_key_in_body_and_normalizes_results() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "T1",
                        "url": "https://a.test/1",
                        "content": "snippet one",
                        "score": 0.9,
                    },
                    {"title": "T2", "url": "https://a.test/2", "content": "snippet two"},
                    "not-a-dict",
                ]
            },
        )

    hits = await TavilySearchProvider().search(
        "eurail night trains",
        api_key="tvly-secret",
        max_results=3,
        transport=httpx.MockTransport(handler),
    )
    assert seen["path"] == "/search"
    assert seen["body"]["api_key"] == "tvly-secret"
    assert seen["body"]["query"] == "eurail night trains"
    assert seen["body"]["max_results"] == 3
    assert [h.url for h in hits] == ["https://a.test/1", "https://a.test/2"]
    assert hits[0].title == "T1" and hits[0].snippet == "snippet one" and hits[0].score == 0.9
    assert hits[1].score is None


async def test_tavily_non_200_is_coarse_and_body_free() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api_key tvly-LEAKED"})

    with pytest.raises(SearchProviderError) as exc:
        await TavilySearchProvider().search(
            "q", api_key="tvly-x", transport=httpx.MockTransport(handler)
        )
    assert exc.value.error_type == "PROVIDER_HTTP_ERROR"
    assert exc.value.status_code == 401
    assert "LEAKED" not in str(exc.value)


async def test_tavily_non_json_is_bad_response() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(SearchProviderError) as exc:
        await TavilySearchProvider().search(
            "q", api_key="tvly-x", transport=httpx.MockTransport(handler)
        )
    assert exc.value.error_type == "PROVIDER_BAD_RESPONSE"


async def test_tavily_missing_results_list_is_bad_response() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answer": "no results key"})

    with pytest.raises(SearchProviderError) as exc:
        await TavilySearchProvider().search(
            "q", api_key="tvly-x", transport=httpx.MockTransport(handler)
        )
    assert exc.value.error_type == "PROVIDER_BAD_RESPONSE"


async def test_tavily_transport_error_is_unreachable() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(SearchProviderError) as exc:
        await TavilySearchProvider().search(
            "q", api_key="tvly-x", transport=httpx.MockTransport(handler)
        )
    assert exc.value.error_type == "PROVIDER_UNREACHABLE"
