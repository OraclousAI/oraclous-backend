"""Web-research connector (domain layer) — the pre-registered live-web tool group.

Three operations behind one curated tool (ADR-039 D1), so an imported live-web researcher runs
immediately (the gap that left EURail's researchers reason-only):

* ``search`` — provider-agnostic web search via the :mod:`search_providers` factory. Resolves a
  per-org BYOM ``api_key`` from the :class:`ExecutionContext` (ADR-038 D3); **key-gated**.
* ``fetch`` — HTTP GET a URL → its raw text body. **Keyless.**
* ``read``  — HTTP GET a URL → readable text (tags/script stripped, ``<title>`` kept). **Keyless.**

Security posture: ``fetch``/``read`` are an SSRF surface (an agent could aim them at an internal
service or the cloud metadata endpoint), so every URL — and every redirect hop — is screened by the
shared :func:`egress_allowed` gate (the same one the MCP connector uses) **before** any request:
http(s)-only, a hostname denylist (``localhost``/``metadata``/``*.internal``/single-label), and a
literal-IP + resolved-IP private/loopback/link-local check. #492: the gate RETURNS the vetted IP and
each hop CONNECTS to that pinned IP (Host + TLS SNI kept as the name via :func:`pinned_request`), so
the connect can't re-resolve to an internal target — the DNS-rebinding TOCTOU is closed. Bodies are
size-capped. No-leak throughout: a missing key, a provider error, a blocked URL, or an upstream 4xx
is a structured failure that never echoes an upstream body. ``transport`` is an injectable seam.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.search_providers import (
    InvalidSiteError,
    SearchProviderError,
    clamp_max_results,
    get_search_provider,
    normalise_sites,
)
from oraclous_capability_registry_service.domain.egress import egress_allowed, pinned_request
from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

# The per-request HTTP timeout sits UNDER the InternalTool hard timeout (``timeout_s`` below) so a
# slow host surfaces the connector's own FETCH_UNREACHABLE, not the wrapper's generic TIMEOUT
# (the same discipline as FederatedSearchConnector).
_FETCH_TIMEOUT_S = 12.0
_OUTER_TIMEOUT_S = 50.0  # headroom for up to _MAX_REDIRECTS sequential hops
_MAX_TEXT_CHARS = 100_000
# The hard refusal ceiling, applied to the raw byte stream DURING download (never after
# buffering the whole body) — well above _MAX_TEXT_CHARS so an ordinary truncation never becomes
# a refusal (#820: the two limits are deliberately not the same control).
_MAX_BODY_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 4
_USER_AGENT = "OraclousWebResearch/1.0"
_OPERATIONS = frozenset({"search", "fetch", "read"})
#: #961 ruling 3: set on a RESTRICTED search that came back with nothing. Named for what it MEANS,
#: not for the status it produces — the runtime decides what an empty restricted search does to a
#: run; this connector only reports what happened. In ``data`` rather than ``metadata`` for the
#: reason ``searched_sites`` is: the execution boundary persists ``result.data`` and drops
#: ``result.metadata`` entirely, so a flag left in metadata would reach nobody.
_SITES_EMPTY_KEY = "sites_yielded_nothing"

# #820: a non-text response is refused rather than decoded — a PDF/image/archive run through
# resp.text (and, worse, the HTML parser on `read`) produces mojibake with the shape of prose,
# and a SourceRef minted over it is indistinguishable from a good one. text/*, the structured
# application/json|xml, and any +json/+xml suffix (e.g. application/ld+json, image/svg+xml) pass;
# a missing or empty header is refused, not guessed (RFC 9110 §8.3's default of
# application/octet-stream is the honest reading for an evidence product).
_STRUCTURED_CONTENT_TYPES = frozenset({"application/json", "application/xml"})


def _is_supported_content_type(content_type: str) -> bool:
    base = content_type.split(";", 1)[0].strip().lower()
    if not base:
        return False
    return (
        base.startswith("text/")
        or base in _STRUCTURED_CONTENT_TYPES
        or base.endswith(("+json", "+xml"))
    )


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


class WebResearchConnector(InternalTool):
    """The ``search`` / ``fetch`` / ``read`` tool group. ``search`` is BYOM-keyed; rest keyless."""

    #: outer hard timeout (InternalTool wrapper); sits ABOVE the per-request fetch timeout so a
    #: single slow hop surfaces FETCH_UNREACHABLE rather than the wrapper's generic TIMEOUT.
    timeout_s: float = _OUTER_TIMEOUT_S

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
        try:
            # Validated BEFORE the credential is resolved and long before the network: a bad address
            # is a bad argument whatever else is true, and the refusal names the offending value so
            # the caller fixes that one rather than guessing. It also means the vendor's own 400
            # (whose body names the value, and which must never be echoed — ADR-008) is not reached.
            sites = normalise_sites(input_data.get("sites"))
        except InvalidSiteError as exc:
            return ExecutionResult(
                success=False, error_message=str(exc), error_type="INVALID_INPUT"
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
                query,
                api_key=str(api_key),
                max_results=max_results,
                sites=sites,
                transport=self.transport,
            )
        except InvalidSiteError as exc:
            # The provider cleans the sites again at the last hop before the vendor. That pass
            # cannot refuse anything this one already accepted, but the guard is what keeps a
            # future caller of the provider from turning a bad argument into a Python class name
            # in the error taxonomy — the exact shape #946 curated away.
            return ExecutionResult(
                success=False, error_message=str(exc), error_type="INVALID_INPUT"
            )
        except SearchProviderError as exc:
            meta = {"status_code": exc.status_code} if exc.status_code is not None else {}
            return ExecutionResult(
                success=False, error_message=str(exc), error_type=exc.error_type, metadata=meta
            )
        data: dict[str, Any] = {"hits": [hit.model_dump() for hit in hits]}
        metadata: dict[str, Any] = {"provider": provider.name, "hit_count": len(hits)}
        if sites:
            # #951 D4b: the run says which hostnames it ACTUALLY searched. No mechanical check can
            # tell `bbc.com` from `bbc.co.uk` — both return real pages — so the only honest answer
            # to a wrong-but-plausible address is to show what was used. These are the CLEANED
            # hostnames, never the raw text supplied, or the mismatch this exists to surface stays
            # hidden. It lives in `data` and not only in `metadata` because the registry's execution
            # boundary persists `result.data` and drops `result.metadata` entirely
            # (`tool_execution_service` finalises with `output_data=result.data`) — a list left in
            # metadata alone would reach neither the member nor the run's step trace.
            data["searched_sites"] = list(sites)
            metadata["searched_sites"] = list(sites)
            if not hits:
                # #961 ruling 3: a reserved key the RUNTIME reads. The sentence below is for the
                # MODEL — it stops the member re-running the identical search — and until now that
                # was all there was, so the run itself settled as an ordinary success and a person
                # never learned their addresses came back empty. This flag is what lets the harness
                # finish the run marked incomplete instead. The harness pops it before the model
                # sees it and believes it only from a first-party search row (#781's posture),
                # which is why it is safe for the two to share one result.
                data[_SITES_EMPTY_KEY] = True
                # Data-absence, not a fault (ADR-021 degrade-not-crash): the named sites simply
                # carry nothing matching. Said as a sentence the member can act on, so it proceeds
                # instead of re-running the identical search. Deliberately NOT the retriever's
                # reserved `data_absent` key: #781 made the runtime believe that only from a trusted
                # retrieval binding, and emitting it here would be exactly the forgery it closed.
                # `note` is also what the runtime writes for a knowledge-retrieval that came back
                # empty. Nothing reads either one — both exist to be READ BY THE MODEL — so the
                # shared name is harmless today, and deliberately so: to a member both mean the
                # same thing, "there was nothing there, carry on". If anything ever starts reading
                # `note`, these two need separating first.
                data["note"] = (
                    "No results were found on the sites this search was restricted to "
                    f"({', '.join(sites)}). Those sites carry nothing matching this query, so "
                    "proceed with what you have rather than repeating the same search. If the "
                    "addresses look wrong, correct them and search once more."
                )
        return ExecutionResult(success=True, data=data, metadata=metadata)

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
        async with httpx.AsyncClient(
            headers=headers,
            timeout=_FETCH_TIMEOUT_S,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                # Screen every hop through the shared SSRF egress gate BEFORE requesting it; manual
                # redirects keep a 3xx from steering the fetch onto an internal/metadata target.
                pinned_ip = await egress_allowed(current)
                if pinned_ip is None:
                    return ExecutionResult(
                        success=False,
                        error_message="the URL is not an allowed public target",
                        error_type="UNSAFE_URL",
                    )
                # #492: dial the vetted PINNED IP (Host + TLS SNI kept as the name), per hop — the
                # connect can't re-resolve to an internal target (DNS-rebinding TOCTOU closed).
                target, host_headers, extensions = pinned_request(current, pinned_ip)
                try:
                    # Streamed (not .get()) so the byte cap can be enforced DURING download —
                    # both the declared Content-Length and the running total, mid-body, before
                    # the whole response is ever materialised (#820).
                    async with client.stream(
                        "GET", target, headers=host_headers, extensions=extensions
                    ) as resp:
                        if resp.is_redirect and resp.headers.get("location"):
                            current = urljoin(current, resp.headers["location"])
                            continue
                        return await self._finish_fetch(resp, url=url, read=read)
                except httpx.HTTPError:
                    return ExecutionResult(
                        success=False,
                        error_message="the URL could not be fetched",
                        error_type="FETCH_UNREACHABLE",
                    )
            else:
                return ExecutionResult(
                    success=False,
                    error_message="too many redirects",
                    error_type="TOO_MANY_REDIRECTS",
                )

    async def _finish_fetch(self, resp: httpx.Response, *, url: str, read: bool) -> ExecutionResult:
        """Validate the final (non-redirect) hop's response and materialise its body.

        Order matters: status, then content-type (before a single body byte is pulled — a typed
        binary or a missing header is refused, never decoded), then the byte cap (checked
        against the declared ``Content-Length`` up front and against the running total while
        streaming, so an over-cap or lying/chunked body is refused mid-download, not after it is
        fully buffered).
        """
        if resp.status_code != 200:
            return ExecutionResult(
                success=False,
                error_message=f"the URL returned {resp.status_code}",
                error_type="FETCH_HTTP_ERROR",
                metadata={"status_code": resp.status_code},
            )
        content_type = resp.headers.get("content-type", "")
        if not _is_supported_content_type(content_type):
            return ExecutionResult(
                success=False,
                error_message=f"unsupported content type: {content_type or '(none)'}",
                error_type="UNSUPPORTED_CONTENT_TYPE",
                metadata={"content_type": content_type},
            )
        declared_length = resp.headers.get("content-length")
        if declared_length is not None and declared_length.isdigit():
            if int(declared_length) > _MAX_BODY_BYTES:
                return ExecutionResult(
                    success=False,
                    error_message="the response body exceeds the size limit",
                    error_type="RESPONSE_TOO_LARGE",
                )
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > _MAX_BODY_BYTES:
                return ExecutionResult(
                    success=False,
                    error_message="the response body exceeds the size limit",
                    error_type="RESPONSE_TOO_LARGE",
                )
            chunks.append(chunk)
        # Cap the raw body ONCE, here — `read`'s extraction below then returns whatever survives
        # whole, rather than trimming the already-trimmed body to the same limit a second time
        # (which used to spend the whole budget on markup a script-heavy page then discards).
        raw_body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
        truncated = len(raw_body) > _MAX_TEXT_CHARS
        body = raw_body[:_MAX_TEXT_CHARS]
        # `truncated` lives in `data`, not only `metadata`: the registry keeps only
        # `result.data` (tool_execution_service.py:209), so a flag that lived solely in
        # `metadata` never reached the model. Present and False on a whole body, not absent —
        # a missing key reads as "unknown", the same ambiguity this is meant to remove.
        if read:
            title, text = _html_to_text(body)
            return ExecutionResult(
                success=True,
                data={"url": url, "title": title, "text": text, "truncated": truncated},
                metadata={"truncated": truncated, "content_type": content_type},
            )
        return ExecutionResult(
            success=True,
            data={"url": url, "content": body, "truncated": truncated},
            metadata={"truncated": truncated, "content_type": content_type},
        )
