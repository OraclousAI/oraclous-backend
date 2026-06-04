"""Retriever factory for knowledge-retriever-service (ORAA-56)."""

from __future__ import annotations

from typing import Any


class RetrieverFactory:
    async def create(self, retriever_type: str, **kwargs: Any) -> Any:
        raise NotImplementedError("factory not yet implemented")
