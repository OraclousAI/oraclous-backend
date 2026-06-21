"""Pluggable web-search providers (ORAA-4 §21 domain layer) — the factory behind ``web.search``.

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
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel

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
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> list[SearchHit]:
        """Run the query with the caller's BYOM key. ``transport`` is an injectable test seam."""


_PROVIDERS: dict[str, type[SearchProvider]] = {}


def register_search_provider(cls: type[SearchProvider]) -> type[SearchProvider]:
    """Register a provider by its ``name`` so the factory can build it. Use as a class decorator."""
    _PROVIDERS[cls.name] = cls
    return cls


def get_search_provider(name: str) -> SearchProvider:
    """Build the provider registered under ``name``; fail-closed on an unknown name."""
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise SearchProviderError(
            f"unknown search provider '{name}'", error_type="UNKNOWN_PROVIDER"
        )
    return cls()


def available_providers() -> list[str]:
    """The registered provider names (stable order) — for diagnostics / the descriptor."""
    return sorted(_PROVIDERS)


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
    """

    name = "tavily"
    base_url = "https://api.tavily.com"

    async def search(
        self,
        query: str,
        *,
        api_key: str,
        max_results: int = _DEFAULT_MAX_RESULTS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> list[SearchHit]:
        body = {
            "api_key": api_key,
            "query": query,
            "max_results": clamp_max_results(max_results),
            "search_depth": "basic",
        }
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
            # coarse status only — the provider's body (which may echo the query/key) never leaks
            raise SearchProviderError(
                f"the search provider returned {resp.status_code}",
                error_type="PROVIDER_HTTP_ERROR",
                status_code=resp.status_code,
            )
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
