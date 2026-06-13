"""Graph CRUD routes (ORAA-4 §21 routes layer).

Handlers are thin: parse the request, make ONE service call, map the result (or a domain error) to
HTTP. No business logic, no DB access, no non-BaseModel classes here (§21 routes rules). The org
scope is already bound by the `bind_org_context` dependency chain behind `get_graph_service`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_graph_service.core.dependencies import GraphServiceDep, UserIdDep
from oraclous_knowledge_graph_service.schema.graph_schemas import (
    CreateGraphRequest,
    GraphResponse,
    UpdateGraphRequest,
)
from oraclous_knowledge_graph_service.services.graph_service import (
    GraphNotFound,
    ReservedGraphName,
)

router = APIRouter(prefix="/api/v1/graphs", tags=["graphs"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")


@router.post("", response_model=GraphResponse, status_code=status.HTTP_201_CREATED)
async def create_graph(
    body: CreateGraphRequest, service: GraphServiceDep, user_id: UserIdDep
) -> GraphResponse:
    try:
        graph = await service.create_graph(
            user_id=user_id, name=body.name, description=body.description
        )
    except ReservedGraphName:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="graph name is reserved for system use",
        ) from None
    return GraphResponse.of(graph)


@router.get("", response_model=list[GraphResponse])
async def list_graphs(service: GraphServiceDep, user_id: UserIdDep) -> list[GraphResponse]:
    graphs = await service.list_graphs(user_id=user_id)
    return [GraphResponse.of(g) for g in graphs]


@router.get("/{graph_id}", response_model=GraphResponse)
async def get_graph(
    graph_id: uuid.UUID, service: GraphServiceDep, user_id: UserIdDep
) -> GraphResponse:
    try:
        graph = await service.get_graph(graph_id=graph_id, user_id=user_id)
    except GraphNotFound:
        raise _NOT_FOUND from None
    return GraphResponse.of(graph)


@router.patch("/{graph_id}", response_model=GraphResponse)
async def update_graph(
    graph_id: uuid.UUID, body: UpdateGraphRequest, service: GraphServiceDep, user_id: UserIdDep
) -> GraphResponse:
    try:
        graph = await service.update_graph(
            graph_id=graph_id, user_id=user_id, name=body.name, description=body.description
        )
    except GraphNotFound:
        raise _NOT_FOUND from None
    return GraphResponse.of(graph)


@router.delete("/{graph_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_graph(graph_id: uuid.UUID, service: GraphServiceDep, user_id: UserIdDep) -> None:
    try:
        await service.delete_graph(graph_id=graph_id, user_id=user_id)
    except GraphNotFound:
        raise _NOT_FOUND from None
