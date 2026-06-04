"""Canonical retrieval orchestration for knowledge-retriever-service (ORAA-56)."""

from __future__ import annotations

from typing import Any


class RetrieverService:
    async def retrieve(self, query: str, **kwargs: Any) -> dict:
        raise NotImplementedError("retrieval not yet implemented")
