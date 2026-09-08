"""Pluggable web-search providers (domain layer) — the factory behind ``web.search``.

The web-research battery's ``search`` operation is provider-agnostic. A :class:`SearchProvider` ABC
defines the contract (``query`` + a per-org BYOM ``api_key`` → a ranked :class:`SearchHit` list);
concrete providers register against a name-keyed factory; the connector picks one by the
``WEB_SEARCH_PROVIDER`` setting (or a per-call ``provider`` override). **Adding a provider is one
subclass + one ``@register_search_provider`` line — no connector change** (Reza's requirement).
Tavily ships first (agent-optimized, freemium); Brave/Serper/etc. slot in identically.

Operator separation (ADR-008): a provider NEVER holds a key. The ``api_key`` is the caller's per-org
BYOM credential, resolved from the :class:`ExecutionContext` per call and never logged or echoed. A
provider failure raises :class:`SearchProviderError` (a coarse, body-free signal) which the
connector maps to a structured failure — an upstream body is never surfaced to the caller.

#951: a search can be restricted to named websites (``sites``). :func:`normalise_sites` strips each
one to a bare hostname first, which is a CORRECTNESS requirement rather than tidiness — a live probe
on 2026-09-07 established that Tavily accepts a full URL in ``include_domains`` with an ordinary 200
and then silently drops the restriction entirely. Sending a pasted address through would rebuild the
exact silent no-op #951 exists to remove, one layer further in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx
from oraclous_ohm.sites import InvalidSiteError, normalise_sites
from pydantic import BaseModel

# #961: the address cleaner moved into the shared kernel and is RE-EXPORTED here, unchanged. Three
# services now compare addresses with each other — the engine reads the person's answer, the harness
# checks the model's call against it, this module sends it to the vendor — and a second copy of the
# rule would put a disagreement between two services rather than two functions (#946's defect, one
# layer up). Both names stay importable from here because this module's callers, and the connector's
# two `except InvalidSiteError` layers, already name them.
__all__ = [
    "InvalidSiteError",
    "SearchHit",
    "SearchProvider",
    "SearchProviderError",
    "normalise_sites",
]

_TIMEOUT_S = 30.0
_DEFAULT_MAX_RESULTS = 5
_MAX_RESULTS_CAP = 20


class SearchHit(BaseModel):
    """One normalized web-search result (provider-independent)."""

    title: str
    url: str
    snippet: str = ""
    score: float | None = None


class SearchProviderError(Exception):
    """A provider call failed: unreachable / auth / bad response. Coarse type, no body echoed."""

    def __init__(self, message: str, *, error_type: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code


class SearchProvider(ABC):
    """Contract for a web-search backend. A provider sets ``name`` and implements ``search``."""

    name: str

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        api_key: str,
        max_results: int = _DEFAULT_MAX_RESULTS,
        sites: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> list[SearchHit]:
        """Run the query with the caller's BYOM key. ``transport`` is an injectable test seam.

        ``sites`` restricts the results to the named websites; absent or empty means an ordinary
        unrestricted search. A provider that cannot express the restriction must refuse rather than
        ignore it — a search that quietly comes back from everywhere is #951's whole complaint.
        """


_PROVIDERS: dict[str, type[SearchProvider]] = {}


def register_search_provider(cls: type[SearchProvider]) -> type[SearchProvider]:
    """Register a provider by its ``name`` so the factory can build it. Use as a class decorator."""
    _PROVIDERS[cls.name] = cls
    return cls


def get_search_provider(name: str) -> SearchProvider:
    """Build the provider registered under ``name``; fail-closed on an unknown name.

    #946: the refusal NAMES what is registered, in the spirit of #899's near-match hint on an
    unknown tool. A bare "unknown search provider 'The Verge'" tells a caller that its value was
    wrong and nothing about what a right one looks like, so the obvious next move is to try another
    website name — which is the retry loop #946 exists to end. Still fail-closed: an unrecognised
    name never falls back to the default vendor (CLAUDE.md §3.5).
    """
    cls = _PROVIDERS.get(name)
    if cls is None:
        registered = ", ".join(available_providers()) or "none"
        raise SearchProviderError(
            f"unknown search provider '{name}' — this argument names the search service to use, "
            f"and the registered ones are: {registered}",
            error_type="UNKNOWN_PROVIDER",
        )
    return cls()


def available_providers() -> list[str]:
    """The registered provider names (stable order) — for diagnostics / the descriptor."""
    return sorted(_PROVIDERS)


# Status -> (error_type, operator-facing sentence). Each sentence names the condition someone can
# act on; none of them quotes the upstream body. 432/433 are Tavily's own plan-limit and rate-limit
# statuses, 429 the standard one — a provider that does not use them simply falls through to the
# coarse label, which is the behaviour every caller had before #875.
_STATUS_CLASSES: dict[int, tuple[str, str]] = {
    432: (
        "PROVIDER_QUOTA_EXHAUSTED",
        "the web-search credential for this organisation has no remaining quota",
    ),
    429: ("PROVIDER_RATE_LIMITED", "the search provider is rate-limiting this organisation"),
    433: ("PROVIDER_RATE_LIMITED", "the search provider is rate-limiting this organisation"),
    401: ("PROVIDER_AUTH_FAILED", "the web-search credential was rejected by the provider"),
    403: (
        "PROVIDER_AUTH_FAILED",
        "the web-search credential is not entitled to make this search",
    ),
}


def classify_provider_status(status_code: int) -> tuple[str, str]:
    """Map a non-200 provider status to ``(error_type, message)`` — status only, never the body.

    An exhausted plan, a throttle and a rejected key each get their own type and their own
    sentence, so a caller above the connector can tell them apart and an operator reading the run
    page knows which one to go fix. Anything unrecognised keeps the pre-existing coarse label.
    """
    known = _STATUS_CLASSES.get(status_code)
    if known is not None:
        return known
    return "PROVIDER_HTTP_ERROR", f"the search provider returned {status_code}"


def clamp_max_results(value: object) -> int:
    """Coerce a caller-supplied ``max_results`` into ``[1, _MAX_RESULTS_CAP]`` (default on junk)."""
    if not isinstance(value, int) or isinstance(value, bool):
        return _DEFAULT_MAX_RESULTS
    return max(1, min(value, _MAX_RESULTS_CAP))


@register_search_provider
class TavilySearchProvider(SearchProvider):
    """Tavily (https://tavily.com) — an LLM/agent-optimized search API. Freemium; one ``api_key``.

    POSTs ``{api_key, query, max_results, search_depth}`` to ``/search`` and normalizes the
    ``results[]`` (``title``/``url``/``content``/``score``) into :class:`SearchHit`. The key travels
    in the request body over HTTPS and is never logged.

    #951: named sites travel as ``include_domains``, the vendor's own parameter, confirmed against
    the live API on 2026-09-07.
    """

    name = "tavily"
    base_url = "https://api.tavily.com"

    async def search(
        self,
        query: str,
        *,
        api_key: str,
        max_results: int = _DEFAULT_MAX_RESULTS,
        sites: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> list[SearchHit]:
        body: dict[str, object] = {
            "api_key": api_key,
            "query": query,
            "max_results": clamp_max_results(max_results),
            "search_depth": "basic",
        }
        # Cleaned again HERE, at the last hop before the vendor, even though the connector already
        # cleaned what it reports. The two are idempotent, and this one is what makes it impossible
        # for any caller — an internal one, a future provider-agnostic path — to hand the vendor a
        # raw URL it would accept with a 200 and then ignore. The key is ABSENT when nothing was
        # named, so an existing caller's request is byte-for-byte what it was before #951.
        restricted_to = normalise_sites(sites)
        if restricted_to:
            body["include_domains"] = restricted_to
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=_TIMEOUT_S,
                transport=transport,
                follow_redirects=False,
            ) as client:
                resp = await client.post("/search", json=body)
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                "the search provider could not be reached", error_type="PROVIDER_UNREACHABLE"
            ) from exc
        if resp.status_code != 200:
            # the status is classified, never the body — an upstream body may echo the query or key
            error_type, message = classify_provider_status(resp.status_code)
            raise SearchProviderError(message, error_type=error_type, status_code=resp.status_code)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SearchProviderError(
                "the search provider returned a non-JSON body", error_type="PROVIDER_BAD_RESPONSE"
            ) from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise SearchProviderError(
                "the search provider returned a malformed body", error_type="PROVIDER_BAD_RESPONSE"
            )
        hits: list[SearchHit] = []
        for row in results:
            if not isinstance(row, dict):
                continue
            raw_score = row.get("score")
            hits.append(
                SearchHit(
                    title=str(row.get("title", "")),
                    url=str(row.get("url", "")),
                    snippet=str(row.get("content", "")),
                    score=float(raw_score) if isinstance(raw_score, (int, float)) else None,
                )
            )
        return hits
