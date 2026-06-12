"""Federated-query DTOs (ORAA-4 §21 schema layer — Pydantic only) — #330 / ADR-026.

`organisation_id` is never an inbound field; the org scope is resolved from the principal.
`graph_ids` is the OPTIONAL explicit subset — validated ∩ accessible, fail-closed; omitted/null
means ALL the caller's accessible graphs. Every result is labeled with its source graph.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class FederatedSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    mode: Literal["entity", "semantic", "fulltext", "hybrid"] = "hybrid"
    # None/omitted = ALL accessible graphs. An explicit subset is validated against the accessible
    # set and FAIL-CLOSED (any unknown/inaccessible id rejects the whole query, 403).
    graph_ids: list[uuid.UUID] | None = None
    per_graph_k: int = Field(default=10, ge=1)  # config-capped (422 above the cap)
    total_k: int = Field(default=50, ge=1)  # config-capped (422 above the cap)


class FederatedSubgraphRequest(BaseModel):
    query: str = Field(min_length=1)
    graph_ids: list[uuid.UUID] | None = None
    entities_per_graph: int = Field(default=5, ge=1)  # anchors matched per graph (config-capped)
    limit_per_graph: int = Field(default=50, ge=1)  # nodes per graph slice (config-capped)


class FederatedNodeResultModel(BaseModel):
    """The NodeResult envelope + the source-graph label every federated hit carries (ADR-026)."""

    id: str
    type: str
    properties: dict[str, Any]
    source_graph_id: str
    source_graph_name: str


class FederatedEdgeResultModel(BaseModel):
    source: str
    target: str
    type: str
    properties: dict[str, Any]
    source_graph_id: str
    source_graph_name: str


class QueriedGraph(BaseModel):
    id: str
    name: str


class FederatedQueryMeta(BaseModel):
    graphs_queried: list[QueriedGraph]
    # Ids beyond the max-graphs cap in default-all mode (never silently dropped). An explicit
    # subset never skips — it either fits the cap or the query is rejected.
    graphs_skipped: list[str]
    mode: str
    # True when the embedder was unavailable/degenerate: semantic contributed nothing, the other
    # modes still served (the clean-degrade path).
    semantic_degraded: bool = False


class FederatedSearchResponse(BaseModel):
    results: list[FederatedNodeResultModel]
    total: int
    meta: FederatedQueryMeta


class FederatedSubgraphResponse(BaseModel):
    nodes: list[FederatedNodeResultModel]
    edges: list[FederatedEdgeResultModel]
    meta: FederatedQueryMeta
