"""Unit: the WebResearchConnector — search (BYOM-keyed), fetch, read, and the SSRF guard.

``search`` resolves a per-org BYOM api_key from the context and dispatches through the provider
factory (Tavily by default); a missing key never reaches the network (fail-closed, typed). ``fetch``
and ``read`` are keyless HTTP, guarded against SSRF (http(s) + public hosts only, re-validated on
every redirect hop). A literal public IP is used for the happy paths so the guard skips real DNS.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.web_research import (
    WebResearchConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000007a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000007c5")
_PUBLIC = "http://1.1.1.1/page"  # a literal public IP → the guard passes without a DNS lookup


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ctx(*, with_key: bool = False) -> ExecutionContext:
    creds = {"api_key": {"api_key": "tvly-secret"}} if with_key else {}
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        execution_id=uuid.uuid4(),
        credentials=creds,
    )


def _connector(handler: Callable[[httpx.Request], httpx.Response]) -> WebResearchConnector:
    ex = WebResearchConnector({"id": "x"})
    ex.transport = httpx.MockTransport(handler)
    return ex


# --- search (BYOM-keyed, via the provider factory) ---------------------------------------------


async def test_search_resolves_byom_key_and_returns_hits() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200, json={"results": [{"title": "T", "url": "https://x.test", "content": "c"}]}
        )

    ex = _connector(handler)
    res = await ex.execute({"operation": "search", "query": "night trains"}, _ctx(with_key=True))
    assert res.success
    assert res.data == {
        "hits": [{"title": "T", "url": "https://x.test", "snippet": "c", "score": None}]
    }
    assert res.metadata == {"provider": "tavily", "hit_count": 1}
    assert seen["path"] == "/search"  # dispatched through Tavily
    assert seen["body"]["api_key"] == "tvly-secret"  # the BYOM key, from the context


async def test_search_without_a_key_fails_closed_before_the_network() -> None:
    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"results": []})

    ex = _connector(handler)
    res = await ex.execute({"operation": "search", "query": "q"}, _ctx(with_key=False))
    assert not res.success and res.error_type == "MISSING_CREDENTIAL"
    assert res.metadata.get("requirement") == "api_key"
    assert called["n"] == 0  # never reached the provider


async def test_search_missing_query_is_rejected() -> None:
    ex = _connector(lambda _r: httpx.Response(200, json={"results": []}))
    res = await ex.execute({"operation": "search"}, _ctx(with_key=True))
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_search_provider_error_is_mapped_without_leaking_the_body() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key tvly-LEAKED"})

    ex = _connector(handler)
    res = await ex.execute({"operation": "search", "query": "q"}, _ctx(with_key=True))
    assert not res.success and res.error_type == "PROVIDER_HTTP_ERROR"
    assert res.metadata.get("status_code") == 401
    assert "LEAKED" not in (res.error_message or "")


async def test_search_unknown_provider_override_fails_closed() -> None:
    ex = _connector(lambda _r: httpx.Response(200, json={"results": []}))
    res = await ex.execute(
        {"operation": "search", "query": "q", "provider": "nope"}, _ctx(with_key=True)
    )
    assert not res.success and res.error_type == "UNKNOWN_PROVIDER"


# --- fetch / read (keyless HTTP) ----------------------------------------------------------------


async def test_fetch_returns_raw_body() -> None:
    ex = _connector(lambda _r: httpx.Response(200, text="hello <b>world</b>"))
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert res.success
    assert res.data == {"url": _PUBLIC, "content": "hello <b>world</b>"}


async def test_read_extracts_title_and_text_stripping_script() -> None:
    html = (
        "<html><head><title> EuRail Pass </title></head>"
        "<body><script>var leak='x'</script><h1>Night trains</h1>"
        "<p>Book ahead in summer.</p></body></html>"
    )
    ex = _connector(lambda _r: httpx.Response(200, text=html))
    res = await ex.execute({"operation": "read", "url": _PUBLIC}, _ctx())
    assert res.success
    assert res.data["title"] == "EuRail Pass"
    assert "Night trains" in res.data["text"] and "Book ahead in summer." in res.data["text"]
    assert "leak" not in res.data["text"]  # <script> contents are dropped


async def test_fetch_non_200_is_a_clean_failure() -> None:
    ex = _connector(lambda _r: httpx.Response(404, text="nope"))
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert not res.success and res.error_type == "FETCH_HTTP_ERROR"
    assert res.metadata.get("status_code") == 404


async def test_fetch_missing_url_is_rejected() -> None:
    ex = _connector(lambda _r: httpx.Response(200, text="x"))
    res = await ex.execute({"operation": "fetch"}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


# --- SSRF guard ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secrets",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/internal",
        "http://192.168.1.1/router",
        "ftp://example.test/x",
        "file:///etc/passwd",
    ],
)
async def test_unsafe_urls_are_refused_before_the_network(url: str) -> None:
    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, text="should-not-happen")

    ex = _connector(handler)
    res = await ex.execute({"operation": "fetch", "url": url}, _ctx())
    assert not res.success and res.error_type == "UNSAFE_URL"
    assert called["n"] == 0


async def test_redirect_to_an_internal_target_is_blocked() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "1.1.1.1":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/meta"})
        return httpx.Response(200, text="LEAKED METADATA")

    ex = _connector(handler)
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert not res.success and res.error_type == "UNSAFE_URL"  # the redirect hop is re-validated
    assert "LEAKED" not in (str(res.data) + (res.error_message or ""))


async def test_invalid_operation_is_rejected() -> None:
    ex = _connector(lambda _r: httpx.Response(200, text="x"))
    res = await ex.execute({"operation": "delete-everything", "url": _PUBLIC}, _ctx())
    assert not res.success and res.error_type == "INVALID_OPERATION"


# --- redirect handling + body caps ---------------------------------------------------------------


async def test_a_safe_single_hop_redirect_is_followed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/a":
            return httpx.Response(302, headers={"location": "http://1.1.1.1/b"})
        return httpx.Response(200, text="final body")

    ex = _connector(handler)
    res = await ex.execute({"operation": "fetch", "url": "http://1.1.1.1/a"}, _ctx())
    assert res.success and res.data["content"] == "final body"


async def test_a_redirect_loop_over_the_cap_fails_closed() -> None:
    hops = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        hops["n"] += 1
        return httpx.Response(302, headers={"location": f"http://1.1.1.1/hop{hops['n']}"})

    ex = _connector(handler)
    res = await ex.execute({"operation": "fetch", "url": "http://1.1.1.1/start"}, _ctx())
    assert not res.success and res.error_type == "TOO_MANY_REDIRECTS"
    assert hops["n"] <= 5  # _MAX_REDIRECTS + 1 attempts, then bail


async def test_fetch_caps_an_oversized_body_and_flags_truncation() -> None:
    big = "x" * 250_000
    ex = _connector(lambda _r: httpx.Response(200, text=big))
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert res.success
    assert len(res.data["content"]) == 100_000  # _MAX_TEXT_CHARS
    assert res.metadata["truncated"] is True


async def test_read_on_a_plaintext_body_returns_the_text() -> None:
    ex = _connector(lambda _r: httpx.Response(200, text="just plain text, no tags"))
    res = await ex.execute({"operation": "read", "url": _PUBLIC}, _ctx())
    assert res.success
    assert res.data["title"] == "" and "just plain text" in res.data["text"]
