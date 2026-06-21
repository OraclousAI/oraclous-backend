"""Web-research connector (ORAA-4 §21 domain layer) — the pre-registered live-web tool group.

Three operations behind one curated tool (ADR-039 D1), so an imported live-web researcher runs
immediately (the gap that left EURail's researchers reason-only):

* ``search`` — provider-agnostic web search via the :mod:`search_providers` factory. Resolves a
  per-org BYOM ``api_key`` from the :class:`ExecutionContext` (ADR-038 D3); **key-gated**.
* ``fetch`` — HTTP GET a URL → its raw text body. **Keyless.**
* ``read``  — HTTP GET a URL → readable text (tags/script stripped, ``<title>`` kept). **Keyless.**

Security posture: ``fetch``/``read`` are an SSRF surface (an agent could aim them at an internal
service or the cloud metadata endpoint), so every URL is validated **before** any network call:
only ``http``/``https`` and only hosts that resolve to public addresses; a private / loopback /
link-local / reserved target is refused fail-closed. Bodies are size-capped. No-leak throughout: a
missing key, a provider error, a blocked URL, or an upstream 4xx is a structured failure that never
echoes an upstream body. ``transport`` is an injectable test seam.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.search_providers import (
    SearchProviderError,
    clamp_max_results,
    get_search_provider,
)
from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

_TIMEOUT_S = 30.0
_MAX_TEXT_CHARS = 100_000
_MAX_REDIRECTS = 4
_USER_AGENT = "OraclousWebResearch/1.0"
_OPERATIONS = frozenset({"search", "fetch", "read"})


class _TextExtractor(HTMLParser):
    """Stdlib HTML → text: drops ``script``/``style``, keeps ``<title>`` and visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._chunks: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        return " ".join(self._chunks)


def _html_to_text(body: str) -> tuple[str, str]:
    """Return ``(title, text)`` extracted from an HTML body (best-effort, dependency-free)."""
    parser = _TextExtractor()
    # HTMLParser is lenient (it does not raise on malformed markup), so a broken page simply
    # degrades to whatever was parsed before the break — no defensive try/except needed.
    parser.feed(body)
    return parser.title.strip(), parser.text()


async def _assert_public_url(url: str) -> None:
    """Fail-closed SSRF guard: http(s) scheme only, and every resolved IP must be public.

    Raises :class:`ValueError` with a coarse reason on a non-public / malformed target. DNS runs
    off the event loop. A host that resolves to *any* private/loopback/link-local/reserved/
    multicast address is refused (defends against DNS-rebinding to an internal / metadata IP).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    host = parts.hostname
    if not host:
        raise ValueError("the URL has no host")
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, parts.port or 0, 0, socket.SOCK_STREAM
        )
    except OSError as exc:
        raise ValueError("the host could not be resolved") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise ValueError("the host could not be resolved")
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise ValueError("the host resolved to an invalid address") from exc
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError("the URL points at a non-public address")


class WebResearchConnector(InternalTool):
    """The ``search`` / ``fetch`` / ``read`` tool group. ``search`` is BYOM-keyed; rest keyless."""

    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        operation = input_data.get("operation", "search")
        if operation not in _OPERATIONS:
            return ExecutionResult(
                success=False,
                error_message=f"'operation' must be one of {sorted(_OPERATIONS)}",
                error_type="INVALID_OPERATION",
            )
        if operation == "search":
            return await self._search(input_data, context)
        return await self._fetch(input_data, read=operation == "read")

    async def _search(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            return ExecutionResult(
                success=False, error_message="'query' is required", error_type="INVALID_INPUT"
            )
        creds = self.get_credentials(context, "api_key")
        api_key = creds.get("api_key") if isinstance(creds, dict) else None
        if not api_key:
            # BYOM: search needs a per-org key (ADR-039 D3). Coarse, typed, no value echoed.
            return ExecutionResult(
                success=False,
                error_message="a web-search api_key credential is required for 'search'",
                error_type="MISSING_CREDENTIAL",
                metadata={"requirement": "api_key"},
            )
        provider_name = input_data.get("provider") or get_settings().WEB_SEARCH_PROVIDER
        max_results = clamp_max_results(input_data.get("max_results"))
        try:
            provider = get_search_provider(str(provider_name))
            hits = await provider.search(
                query, api_key=str(api_key), max_results=max_results, transport=self.transport
            )
        except SearchProviderError as exc:
            meta = {"status_code": exc.status_code} if exc.status_code is not None else {}
            return ExecutionResult(
                success=False, error_message=str(exc), error_type=exc.error_type, metadata=meta
            )
        return ExecutionResult(
            success=True,
            data={"hits": [hit.model_dump() for hit in hits]},
            metadata={"provider": provider.name, "hit_count": len(hits)},
        )

    async def _fetch(self, input_data: dict[str, Any], *, read: bool) -> ExecutionResult:
        url = input_data.get("url")
        if not isinstance(url, str) or not url.strip():
            return ExecutionResult(
                success=False, error_message="'url' is required", error_type="INVALID_INPUT"
            )
        # Redirects are followed MANUALLY so every hop is SSRF-re-validated before it is requested;
        # auto-following would let an external page 302 the fetch onto an internal/metadata target.
        headers = {"User-Agent": _USER_AGENT}
        current = url
        resp: httpx.Response | None = None
        async with httpx.AsyncClient(
            headers=headers, timeout=_TIMEOUT_S, transport=self.transport, follow_redirects=False
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                try:
                    await _assert_public_url(current)
                except ValueError as exc:
                    return ExecutionResult(
                        success=False, error_message=str(exc), error_type="UNSAFE_URL"
                    )
                try:
                    resp = await client.get(current)
                except httpx.HTTPError:
                    return ExecutionResult(
                        success=False,
                        error_message="the URL could not be fetched",
                        error_type="FETCH_UNREACHABLE",
                    )
                if resp.is_redirect and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                break
            else:
                return ExecutionResult(
                    success=False,
                    error_message="too many redirects",
                    error_type="TOO_MANY_REDIRECTS",
                )
        if resp is None:  # defensive: the loop always sets resp or returns before here
            return ExecutionResult(
                success=False,
                error_message="the URL could not be fetched",
                error_type="FETCH_UNREACHABLE",
            )
        if resp.status_code != 200:
            return ExecutionResult(
                success=False,
                error_message=f"the URL returned {resp.status_code}",
                error_type="FETCH_HTTP_ERROR",
                metadata={"status_code": resp.status_code},
            )
        body = resp.text
        truncated = len(body) > _MAX_TEXT_CHARS
        body = body[:_MAX_TEXT_CHARS]
        content_type = resp.headers.get("content-type", "")
        if read:
            title, text = _html_to_text(body)
            return ExecutionResult(
                success=True,
                data={"url": url, "title": title, "text": text[:_MAX_TEXT_CHARS]},
                metadata={"truncated": truncated, "content_type": content_type},
            )
        return ExecutionResult(
            success=True,
            data={"url": url, "content": body},
            metadata={"truncated": truncated, "content_type": content_type},
        )
