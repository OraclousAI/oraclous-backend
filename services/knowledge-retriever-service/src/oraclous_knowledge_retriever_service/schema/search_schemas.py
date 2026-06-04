"""Retrieval DTOs (ORAA-4 §21 schema layer — Pydantic only)."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    graph_id: uuid.UUID
    top_k: int = Field(default=10, ge=1, le=100)


class NodeResultModel(BaseModel):
    """The canonical retrieval envelope — modality data lives inside `properties`."""

    id: str
    type: str
    properties: dict[str, Any]


class HealthResponse(BaseModel):
    status: str
    service: str
