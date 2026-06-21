"""Pluggable REST data sources (#489) — the factory behind the generic REST connector.

A SourceProvider declares a base_url, a {endpoint -> path} map, and a parse() that normalizes
the endpoint's response into a dict. Concrete providers register against a name-keyed factory; the
connector picks one by the request's source_id. Adding a source is one subclass + one
@register_source_provider line, no connector change (the same shape as the web-search provider
factory). The two shipped providers are keyless public GETs, so the deployed proof needs no BYOM
setup; keyed sources slot in identically with auth="api_key".
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod


class SourceProviderError(Exception):
    """A source/endpoint lookup or response-parse failed. Carries a coarse, body-free type."""

    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class SourceProvider(ABC):
    """A curated REST data source: its base URL, endpoint→path map, and a response parser."""

    name: str
    base_url: str
    auth: str = "none"  # none | api_key | bearer
    endpoints: dict[str, str]

    def path_for(self, endpoint: str) -> str | None:
        """The path for ``endpoint``, or ``None`` if the source does not declare it."""
        return self.endpoints.get(endpoint)

    @abstractmethod
    def parse(self, endpoint: str, text: str) -> dict:
        """Normalize the endpoint's response body into a dict; raise ValueError on a bad shape."""


_PROVIDERS: dict[str, type[SourceProvider]] = {}


def register_source_provider(cls: type[SourceProvider]) -> type[SourceProvider]:
    """Register a source by its ``name`` so the factory can build it. Use as a class decorator."""
    _PROVIDERS[cls.name] = cls
    return cls


def get_source_provider(name: str) -> SourceProvider:
    """Build the source registered under ``name``; fail-closed on an unknown name."""
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise SourceProviderError(f"unknown source '{name}'", error_type="UNKNOWN_SOURCE")
    return cls()


def available_sources() -> list[str]:
    """The registered source names (stable order) — for diagnostics / the descriptor."""
    return sorted(_PROVIDERS)


@register_source_provider
class MempoolProvider(SourceProvider):
    """mempool.space — public Bitcoin chain data, keyless. ``tip_height`` returns the chain tip."""

    name = "mempool"
    base_url = "https://mempool.space"
    endpoints = {"tip_height": "/api/blocks/tip/height"}

    def parse(self, endpoint: str, text: str) -> dict:
        return {"block_height": int(text.strip())}


@register_source_provider
class AlternativeMeProvider(SourceProvider):
    """alternative.me — the Crypto Fear & Greed index, keyless. ``fear_greed`` = the latest."""

    name = "alternative_me"
    base_url = "https://api.alternative.me"
    endpoints = {"fear_greed": "/fng/"}

    def parse(self, endpoint: str, text: str) -> dict:
        payload = json.loads(text)
        latest = payload["data"][0]
        return {
            "value": int(latest["value"]),
            "classification": str(latest["value_classification"]),
        }
