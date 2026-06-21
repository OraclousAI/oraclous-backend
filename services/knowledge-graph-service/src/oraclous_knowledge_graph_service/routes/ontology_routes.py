"""Ontology routes (routes layer) — get/set a graph's label ontology (owner scoped).

Slice C adds a graph-independent authoring aid: ``POST /api/v1/ontology/suggest`` infers a typed
ontology from a text sample (schema synthesis) and returns it in the SAME shape the per-graph
ontology PUT accepts, so a client can review and then save it onto a graph.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.dependencies import OntologyServiceDep, UserIdDep
from oraclous_knowledge_graph_service.schema.ontology_schemas import (
    OntologyRequest,
    OntologyResponse,
    SuggestedOntologyResponse,
    SuggestOntologyRequest,
)
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound
from oraclous_knowledge_graph_service.services.ontology_service import OntologyError
from oraclous_knowledge_graph_service.services.schema_synthesis_service import (
    SchemaSynthesisUnavailable,
    make_synthesizer,
)

router = APIRouter(prefix="/api/v1/graphs/{graph_id}/ontology", tags=["ontology"])

# A second router for the graph-independent authoring aid (not nested under a graph id).
suggest_router = APIRouter(prefix="/api/v1/ontology", tags=["ontology"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")


@suggest_router.post("/suggest", response_model=SuggestedOntologyResponse)
async def suggest_ontology(
    body: SuggestOntologyRequest, _user_id: UserIdDep
) -> SuggestedOntologyResponse:
    """Infer a typed ontology from a text sample (schema synthesis). 503 when no LLM is configured.

    The synthesizer is built inside the handler (not as a dependency) so its fail-closed
    ``SchemaSynthesisUnavailable`` maps cleanly to 503 instead of escaping dependency resolution.
    """
    try:
        synthesizer = make_synthesizer(get_settings())
    except SchemaSynthesisUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    suggestion = await synthesizer.suggest(sample=body.sample, mode=body.mode)
    return SuggestedOntologyResponse(**suggestion)


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
            user_id=user_id,
            graph_id=graph_id,
            allowed_labels=body.allowed_labels,
            mode=body.mode,
            entity_types=[e.model_dump() for e in body.entity_types],
            relationship_types=[r.model_dump() for r in body.relationship_types],
            domain=body.domain,
            density=body.density,
            focus=body.focus,
            ignore=body.ignore,
        )
    except GraphNotFound:
        raise _NOT_FOUND from None
    except OntologyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return OntologyResponse(**data)
