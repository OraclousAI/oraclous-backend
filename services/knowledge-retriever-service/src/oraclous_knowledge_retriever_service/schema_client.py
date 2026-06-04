"""KGS schema client with read-through cache and circuit-breaker (ORAA-57).

Circuit-breaker contract:
  - 200 from KGS → return schema dict, populate/refresh cache
  - timeout or ConnectError + cache hit → return cached copy
  - timeout or ConnectError + no cache → raise SchemaUnavailable
  - 404 from KGS → raise SchemaNotFound (never masked by cache)
"""

from __future__ import annotations

import httpx
from httpx import ConnectError as _ConnectError
from httpx import TimeoutException as _TimeoutException


class SchemaUnavailable(Exception):
    """KGS is unreachable and no cached schema exists for this graph."""


class SchemaNotFound(Exception):
    """KGS returned 404 — the graph schema does not exist."""


class SchemaClient:
    def __init__(self, kgs_base_url: str, cache: dict | None = None) -> None:
        self._base_url = kgs_base_url.rstrip("/")
        self._cache: dict = cache if cache is not None else {}

    async def get_schema(self, graph_id: str) -> dict:
        url = f"{self._base_url}/internal/v1/schema/{graph_id}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
        except (_TimeoutException, _ConnectError):
            if graph_id in self._cache:
                return self._cache[graph_id]
            raise SchemaUnavailable(graph_id) from None

        if response.status_code == 404:
            raise SchemaNotFound(graph_id)

        schema = response.json()
        self._cache[graph_id] = schema
        return schema
