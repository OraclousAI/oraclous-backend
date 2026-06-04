"""Fulltext index lifecycle service for knowledge-retriever-service (ORAA-56)."""

from __future__ import annotations

from typing import Any


class FulltextIndexService:
    async def build(self, graph_id: str, **kwargs: Any) -> None:
        raise NotImplementedError("fulltext index not yet implemented")
