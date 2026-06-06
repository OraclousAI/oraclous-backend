"""Graph traversal + temporal routes (ORAA-4 §21 routes layer).

GET /v1/graph/{graph_id}/subgraph?limit=... — a bounded {nodes, edges} slice for visualisation.
GET /v1/graph/{graph_id}/neighbors/{node_id} — 1-hop neighbourhood of a node.
GET /v1/graph/{graph_id}/temporal?as_of=... — entities whose validity covers `as_of`.
All org+graph scoped.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from oraclous_knowledge_retriever_service.core.dependencies import RetrievalServiceDep, UserIdDep
from oraclous_knowledge_retriever_service.schema.search_schemas import (
    NodeResultModel,
    SubgraphResultModel,
)

router = APIRouter(prefix="/v1/graph", tags=["graph"])


@router.get("/{graph_id}/subgraph", response_model=SubgraphResultModel)
async def subgraph(
    graph_id: uuid.UUID,
    service: RetrievalServiceDep,
    _user_id: UserIdDep,
    limit: int = Query(default=250, ge=1, le=1000),
) -> SubgraphResultModel:
    result = await service.subgraph(graph_id=str(graph_id), limit=limit)
    return SubgraphResultModel(**result)


@router.get("/{graph_id}/neighbors/{node_id}", response_model=list[NodeResultModel])
async def neighbors(
    graph_id: uuid.UUID,
    node_id: str,
    service: RetrievalServiceDep,
    _user_id: UserIdDep,
    top_k: int = Query(default=25, ge=1, le=200),
) -> list[NodeResultModel]:
    results = await service.neighbors(graph_id=str(graph_id), node_id=node_id, top_k=top_k)
    return [NodeResultModel(**r) for r in results]


@router.get("/{graph_id}/temporal", response_model=list[NodeResultModel])
async def temporal(
    graph_id: uuid.UUID,
    service: RetrievalServiceDep,
    _user_id: UserIdDep,
    as_of: str = Query(..., description="ISO timestamp; entities valid at this instant"),
    top_k: int = Query(default=25, ge=1, le=200),
) -> list[NodeResultModel]:
    results = await service.temporal(graph_id=str(graph_id), as_of=as_of, top_k=top_k)
    return [NodeResultModel(**r) for r in results]
