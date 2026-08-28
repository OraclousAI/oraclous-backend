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
        return httpx.Response(500, json={"error": "upstream blew up on tvly-LEAKED"})

    ex = _connector(handler)
    res = await ex.execute({"operation": "search", "query": "q"}, _ctx(with_key=True))
    assert not res.success and res.error_type == "PROVIDER_HTTP_ERROR"
    assert res.metadata.get("status_code") == 500
    assert "LEAKED" not in (res.error_message or "")


# --- #875: the connector hands the CLASSIFIED failure onward, it does not re-flatten it ---------


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [
        (432, "PROVIDER_QUOTA_EXHAUSTED"),
        (429, "PROVIDER_RATE_LIMITED"),
        (433, "PROVIDER_RATE_LIMITED"),
        (401, "PROVIDER_AUTH_FAILED"),
        (403, "PROVIDER_AUTH_FAILED"),
        (500, "PROVIDER_HTTP_ERROR"),
    ],
)
async def test_search_passes_the_classified_failure_through(
    status: int, expected_type: str
) -> None:
    """Whatever the provider decided a status means, the caller above still sees it."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "tvly-LEAKED", "query": "SECRET-QUERY"})

    ex = _connector(handler)
    res = await ex.execute({"operation": "search", "query": "q"}, _ctx(with_key=True))
    assert not res.success
    assert res.error_type == expected_type
    assert res.metadata.get("status_code") == status
    message = res.error_message or ""
    assert "LEAKED" not in message and "SECRET-QUERY" not in message


async def test_exhausted_quota_is_not_reported_as_a_missing_credential() -> None:
    """The key IS present and IS valid — it just has no credit left. Confusing the two sends the
    operator to re-enter a key that was never the problem (live 2026-08-25, run eb08c17d)."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(432, json={})

    ex = _connector(handler)
    res = await ex.execute({"operation": "search", "query": "q"}, _ctx(with_key=True))
    assert res.error_type not in {"MISSING_CREDENTIAL", "PROVIDER_AUTH_FAILED"}
    assert res.error_type == "PROVIDER_QUOTA_EXHAUSTED"


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
    # `truncated` joined `data` in #820 (metadata never reaches the model), so this asserts the
    # two fields it is actually about rather than pinning the whole dict. An exact-equality
    # assertion on a payload makes every future addition to that payload a test failure.
    assert res.data["url"] == _PUBLIC
    assert res.data["content"] == "hello <b>world</b>"


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


async def test_fetch_dials_the_pinned_ip_with_host_and_sni_across_a_redirect_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #492 TOCTOU: a hostname fetch + a hostname redirect must each connect to the guard's PINNED IP
    # (not re-resolve), preserving Host/SNI = the original name — on EVERY hop.
    pins = {"a.example.com": "93.184.216.34", "b.example.com": "198.51.100.7"}

    async def fake_egress(url: str, **_kw: object) -> str | None:
        from urllib.parse import urlparse

        return pins.get(urlparse(url).hostname or "")

    monkeypatch.setattr(
        "oraclous_capability_registry_service.domain.connectors.web_research.egress_allowed",
        fake_egress,
    )
    seen: list[tuple[str, str | None, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.host, req.headers.get("host"), req.extensions.get("sni_hostname")))
        if req.headers.get("host") == "a.example.com":
            return httpx.Response(302, headers={"location": "https://b.example.com/next"})
        return httpx.Response(200, text="final")

    ex = _connector(handler)
    res = await ex.execute({"operation": "fetch", "url": "https://a.example.com/start"}, _ctx())
    assert res.success and res.data["content"] == "final"
    # hop 1 dialed a's pinned IP; hop 2 dialed b's pinned IP — each with Host/SNI = its real name
    assert seen[0] == ("93.184.216.34", "a.example.com", "a.example.com")
    assert seen[1] == ("198.51.100.7", "b.example.com", "b.example.com")


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


# --- #820 content-type gate ---------------------------------------------------------------------
#
# The connector reads `content-type` today only to echo it into metadata, and metadata is dropped
# before the model ever sees it (`tool_execution_service.py:209` keeps `result.data` alone). So a
# PDF is decoded by `resp.text` and, on `read`, run through the HTML parser. What comes back is
# mojibake with the shape of prose — and a SourceRef minted over it is indistinguishable from a
# good one. For an evidence product that is the worst available failure mode, because the citation
# gate checks that the identifier was served, never what the content was.


def _typed(content_type: str, body: bytes = b"%PDF-1.4 binary") -> Callable[..., httpx.Response]:
    return lambda _r: httpx.Response(200, content=body, headers={"content-type": content_type})


@pytest.mark.parametrize(
    "content_type",
    [
        "application/pdf",
        "image/png",
        "application/zip",
        "application/octet-stream",
        "audio/mpeg",
        "video/mp4",
        "font/woff2",
    ],
)
async def test_fetch_refuses_a_binary_content_type(content_type: str) -> None:
    ex = _connector(_typed(content_type))
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert not res.success, f"{content_type} was decoded as text"
    assert res.error_type == "UNSUPPORTED_CONTENT_TYPE"
    # The caller is told WHAT was served, so "fetch the PDF" is an actionable next step rather
    # than a dead end (the #692/#693 actionable-error discipline).
    assert content_type in res.error_message
    assert res.metadata.get("content_type") == content_type


async def test_read_refuses_a_binary_content_type() -> None:
    """`read` is the worse half: the HTML parser turns decoded binary into plausible prose."""
    ex = _connector(_typed("application/pdf"))
    res = await ex.execute({"operation": "read", "url": _PUBLIC}, _ctx())
    assert not res.success and res.error_type == "UNSUPPORTED_CONTENT_TYPE"


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html; charset=utf-8",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/xml",
        "application/ld+json",
        "image/svg+xml",
        "APPLICATION/JSON",  # the header is case-insensitive
    ],
)
async def test_fetch_accepts_text_and_the_structured_types(content_type: str) -> None:
    ex = _connector(_typed(content_type, body=b"<p>hello</p>"))
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert res.success, f"{content_type} was refused: {res.error_message}"
    assert res.data["content"] == "<p>hello</p>"


async def test_fetch_refuses_a_response_with_no_content_type() -> None:
    """An unlabelled body is refused, not guessed.

    RFC 9110 §8.3 says a recipient that gets no Content-Type may assume
    ``application/octet-stream``, and that is the honest reading here: for an evidence product an
    unlabelled body is exactly the ambiguity we cannot resolve, and guessing "probably HTML" is
    how binary reaches the model. Flagged for the tests reviewer as a deliberate ruling — the
    alternative (default to text) trades a small compatibility win for the defect this issue is
    about.
    """
    ex = _connector(lambda _r: httpx.Response(200, content=b"\x89PNG\r\n", headers={}))
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert not res.success and res.error_type == "UNSUPPORTED_CONTENT_TYPE"


# --- #820 byte cap, applied during download -----------------------------------------------------


async def test_fetch_refuses_an_over_cap_content_length_without_reading_the_body() -> None:
    """A declared over-cap length is refused before a single body byte is pulled.

    Today the only bound on a multi-gigabyte response is the 12-second timeout, and the whole body
    is materialised (`resp.text`) before the 100k-character trim ever runs. Checking the declared
    length first is the cheapest half of the fix.
    """
    from oraclous_capability_registry_service.domain.connectors.web_research import _MAX_BODY_BYTES

    pulled = {"n": 0}

    async def _body():
        pulled["n"] += 1
        yield b"x" * 1024

    ex = _connector(
        lambda _r: httpx.Response(
            200,
            content=_body(),
            headers={
                "content-type": "text/html",
                "content-length": str(_MAX_BODY_BYTES + 1),
            },
        )
    )
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert not res.success and res.error_type == "RESPONSE_TOO_LARGE"
    assert pulled["n"] == 0, "the body was read despite an over-cap Content-Length"


async def test_fetch_stops_pulling_a_chunked_body_once_it_passes_the_cap() -> None:
    """No Content-Length is the case that matters, and it must be caught mid-stream.

    A server that omits the header (or lies) is the reason the cap has to be applied DURING the
    download rather than after it. The assertion is that the connector stopped early — not merely
    that it reported an error once the whole body was in memory.
    """
    from oraclous_capability_registry_service.domain.connectors.web_research import _MAX_BODY_BYTES

    chunk = b"x" * 65_536
    total_chunks = (_MAX_BODY_BYTES // len(chunk)) * 4
    served = {"n": 0}

    async def _body():
        for _ in range(total_chunks):
            served["n"] += 1
            yield chunk

    ex = _connector(
        lambda _r: httpx.Response(200, content=_body(), headers={"content-type": "text/html"})
    )
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert not res.success and res.error_type == "RESPONSE_TOO_LARGE"
    assert served["n"] < total_chunks, "the whole body was buffered before the cap was applied"


async def test_the_byte_cap_sits_above_the_character_truncation_limit() -> None:
    """The two limits are different controls and must not be collapsed into one.

    Over the byte cap is a refusal (the caller gets nothing and is told why). Over the character
    limit is a truncation (the caller gets a usable prefix and is told it is a prefix). Ordering
    them the other way round would make every truncation a refusal.
    """
    from oraclous_capability_registry_service.domain.connectors.web_research import (
        _MAX_BODY_BYTES,
        _MAX_TEXT_CHARS,
    )

    assert _MAX_BODY_BYTES > _MAX_TEXT_CHARS


# --- #820 truncation has to reach the caller ----------------------------------------------------


async def test_fetch_reports_truncation_in_data_not_only_metadata() -> None:
    """`ExecutionResult.metadata` never reaches the model.

    The registry keeps `result.data` alone (`tool_execution_service.py:209`), so a `truncated`
    flag that lives in metadata is a flag nobody can read. An agent that cannot tell a whole page
    from the first 100k characters of one will cite the page as if it had read all of it.
    """
    ex = _connector(lambda _r: httpx.Response(200, text="x" * 250_000))
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert res.success
    assert res.data["truncated"] is True


async def test_fetch_reports_truncated_false_on_a_whole_body() -> None:
    # Present and False, not absent: a missing key reads as "unknown", which is the same ambiguity.
    ex = _connector(lambda _r: httpx.Response(200, text="short"))
    res = await ex.execute({"operation": "fetch", "url": _PUBLIC}, _ctx())
    assert res.success and res.data["truncated"] is False


async def test_read_reports_truncation_in_data() -> None:
    ex = _connector(lambda _r: httpx.Response(200, text="<p>" + "x" * 250_000 + "</p>"))
    res = await ex.execute({"operation": "read", "url": _PUBLIC}, _ctx())
    assert res.success and res.data["truncated"] is True


async def test_read_does_not_truncate_the_extracted_text_a_second_time() -> None:
    """`read` trims twice today: the body to 100k, then the extracted text to 100k again.

    On a script-heavy page the first trim can consume the budget on markup the reader then drops,
    so the caller sees far less visible text than the limit implies. The raw body is the thing
    that gets capped; whatever survives extraction is returned whole.
    """
    from oraclous_capability_registry_service.domain.connectors.web_research import _MAX_TEXT_CHARS

    visible = "y" * 5_000
    padding = "<span>" + "z" * (_MAX_TEXT_CHARS - 6_000) + "</span>"
    html = f"<html><body><p>{visible}</p>{padding}</body></html>"
    ex = _connector(lambda _r: httpx.Response(200, text=html))
    res = await ex.execute({"operation": "read", "url": _PUBLIC}, _ctx())
    assert res.success
    assert res.data["truncated"] is False, "a body under the cap must not be reported as truncated"
    assert visible in res.data["text"]
