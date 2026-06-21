"""Agent-memory routes (routes layer) — issue #332 / ADR-027 §4.

Six endpoints mirroring the legacy shapes under ``/api/v1/graphs/{graph_id}/memories``:

  POST   …/memories               store (201; returns contradictions_detected)
  GET    …/memories/search        hybrid recall (query, type/scope/temporal/min_confidence/limit)
  GET    …/memories/context       token-budgeted "## Relevant Memory" markdown block
  PATCH  …/memories/{memory_id}   supersede (temporal versioning)
  DELETE …/memories/{memory_id}   forget (soft default; ?hard=true detach-deletes)
  POST   …/memories/consolidate   enqueue the similarity-consolidation job

Handlers are thin: parse → ONE service call → map domain errors to HTTP (§21). The org scope is
bound by the dependency chain behind ``MemoryServiceDep``; graph visibility is the service's
org-scoped gate (another org's graph → 404, no leak).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from oraclous_knowledge_graph_service.core.dependencies import MemoryServiceDep, UserIdDep
from oraclous_knowledge_graph_service.schema.memory_schemas import (
    ConsolidateResponse,
    MemoryContext,
    MemoryCreate,
    MemoryCreateResponse,
    MemoryScope,
    MemorySearchResponse,
    MemoryType,
    MemoryUpdate,
    MemoryUpdateResponse,
    TemporalFilter,
)
from oraclous_knowledge_graph_service.services.memory_service import (
    GraphNotVisible,
    MemoryNotFound,
)

router = APIRouter(prefix="/api/v1/graphs/{graph_id}/memories", tags=["memories"])

_GRAPH_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")
_MEMORY_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory not found")


@router.post("", response_model=MemoryCreateResponse, status_code=status.HTTP_201_CREATED)
async def store_memory(
    graph_id: uuid.UUID, body: MemoryCreate, service: MemoryServiceDep, _user_id: UserIdDep
) -> MemoryCreateResponse:
    try:
        return await service.store(graph_id=graph_id, req=body)
    except GraphNotVisible:
        raise _GRAPH_NOT_FOUND from None


@router.get("/search", response_model=MemorySearchResponse)
async def search_memories(
    graph_id: uuid.UUID,
    service: MemoryServiceDep,
    _user_id: UserIdDep,
    query: str = Query(min_length=1, description="Search query"),
    type: Annotated[  # noqa: A002 — legacy param name
        MemoryType | None, Query(description="Filter by memory type")
    ] = None,
    scope: Annotated[MemoryScope | None, Query(description="Filter by scope")] = None,
    temporal: Annotated[TemporalFilter, Query()] = TemporalFilter.CURRENT,
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    limit: int = Query(default=20, ge=1, le=100),
) -> MemorySearchResponse:
    try:
        return await service.search(
            graph_id=graph_id,
            query=query,
            memory_type=type,
            scope=scope.value if scope else None,
            temporal=temporal.value,
            min_confidence=min_confidence,
            limit=limit,
        )
    except GraphNotVisible:
        raise _GRAPH_NOT_FOUND from None


@router.get("/context", response_model=MemoryContext)
async def get_memory_context(
    graph_id: uuid.UUID,
    service: MemoryServiceDep,
    _user_id: UserIdDep,
    query: str = Query(min_length=1, description="Current agent query or topic"),
    scope: str | None = Query(default=None, description="Comma-separated scopes"),
    max_tokens: int = Query(default=2000, ge=100, le=8000),
    include_types: str | None = Query(default=None, description="Comma-separated memory types"),
) -> MemoryContext:
    scopes = [s.strip() for s in scope.split(",") if s.strip()] if scope else None
    types = [t.strip() for t in include_types.split(",") if t.strip()] if include_types else None
    try:
        return await service.context(
            graph_id=graph_id,
            query=query,
            scopes=scopes,
            max_tokens=max_tokens,
            include_types=types,
        )
    except GraphNotVisible:
        raise _GRAPH_NOT_FOUND from None


@router.patch("/{memory_id}", response_model=MemoryUpdateResponse)
async def update_memory(
    graph_id: uuid.UUID,
    memory_id: str,
    body: MemoryUpdate,
    service: MemoryServiceDep,
    _user_id: UserIdDep,
) -> MemoryUpdateResponse:
    try:
        return await service.supersede(graph_id=graph_id, memory_id=memory_id, req=body)
    except GraphNotVisible:
        raise _GRAPH_NOT_FOUND from None
    except MemoryNotFound:
        raise _MEMORY_NOT_FOUND from None


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    graph_id: uuid.UUID,
    memory_id: str,
    service: MemoryServiceDep,
    _user_id: UserIdDep,
    hard: bool = Query(default=False, description="Hard delete removes the node entirely"),
) -> None:
    try:
        await service.delete(graph_id=graph_id, memory_id=memory_id, hard=hard)
    except GraphNotVisible:
        raise _GRAPH_NOT_FOUND from None
    except MemoryNotFound:
        raise _MEMORY_NOT_FOUND from None


@router.post("/consolidate", response_model=ConsolidateResponse, status_code=202)
async def consolidate_memories(
    graph_id: uuid.UUID, service: MemoryServiceDep, _user_id: UserIdDep
) -> ConsolidateResponse:
    try:
        job_id = await service.consolidate(graph_id=graph_id)
    except GraphNotVisible:
        raise _GRAPH_NOT_FOUND from None
    return ConsolidateResponse(
        job_id=job_id, message=f"Consolidation job queued for graph {graph_id}"
    )
