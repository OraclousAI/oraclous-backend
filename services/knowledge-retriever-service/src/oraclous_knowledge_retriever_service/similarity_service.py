"""Vector similarity primitives for knowledge-retriever-service (ORAA-56)."""

from __future__ import annotations

from typing import Any


class SimilarityService:
    async def compute(self, node_id: str, **kwargs: Any) -> dict:
        raise NotImplementedError("similarity not yet implemented")
