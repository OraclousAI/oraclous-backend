"""Ontology routes (ORAA-4 §21 routes layer) — get/set a graph's label ontology (owner scoped)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_graph_service.core.dependencies import OntologyServiceDep, UserIdDep
from oraclous_knowledge_graph_service.schema.ontology_schemas import (
    OntologyRequest,
    OntologyResponse,
)
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound
from oraclous_knowledge_graph_service.services.ontology_service import OntologyError

router = APIRouter(prefix="/api/v1/graphs/{graph_id}/ontology", tags=["ontology"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")


@router.get("", response_model=OntologyResponse)
async def get_ontology(
    graph_id: uuid.UUID, service: OntologyServiceDep, user_id: UserIdDep
) -> OntologyResponse:
    try:
        data = await service.get(user_id=user_id, graph_id=graph_id)
    except GraphNotFound:
        raise _NOT_FOUND from None
    return OntologyResponse(**data)


@router.put("", response_model=OntologyResponse)
async def set_ontology(
    graph_id: uuid.UUID, body: OntologyRequest, service: OntologyServiceDep, user_id: UserIdDep
) -> OntologyResponse:
    try:
        data = await service.set(
            user_id=user_id, graph_id=graph_id, allowed_labels=body.allowed_labels, mode=body.mode
        )
    except GraphNotFound:
        raise _NOT_FOUND from None
    except OntologyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return OntologyResponse(**data)
