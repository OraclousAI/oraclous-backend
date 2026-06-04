"""Liveness route (ORAA-4 §21 routes layer). Unauthenticated — docker healthcheck."""

from __future__ import annotations

from fastapi import APIRouter

from oraclous_knowledge_retriever_service.schema.search_schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="knowledge-retriever-service")
