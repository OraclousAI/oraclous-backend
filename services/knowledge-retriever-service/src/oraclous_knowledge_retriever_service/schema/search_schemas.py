"""Retrieval DTOs (schema layer — Pydantic only)."""

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


class EdgeResultModel(BaseModel):
    """A directed relationship between two nodes in a subgraph; endpoints are node ids.

    Edge-level data (e.g. a `score` on SIMILAR_TO/SAME_AS_CANDIDATE) lives inside
    `properties` — mirroring `NodeResultModel`, so the FE explorer can read it.
    """

    source: str
    target: str
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class SubgraphResultModel(BaseModel):
    """A bounded graph slice for visualisation: capped nodes + the edges among them."""

    nodes: list[NodeResultModel]
    edges: list[EdgeResultModel]


class HealthResponse(BaseModel):
    status: str
    service: str
