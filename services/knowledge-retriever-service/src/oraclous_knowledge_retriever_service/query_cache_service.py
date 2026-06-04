"""Query cache service for knowledge-retriever-service (ORAA-56)."""

from __future__ import annotations

from typing import Any


class QueryCacheService:
    async def get(self, key: str, **kwargs: Any) -> Any:
        raise NotImplementedError("cache not yet implemented")
