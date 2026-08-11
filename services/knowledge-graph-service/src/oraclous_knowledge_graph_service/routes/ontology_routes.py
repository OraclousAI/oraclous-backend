"""Ontology routes (routes layer) — get/set a graph's label ontology (owner scoped).

Slice C adds a graph-independent authoring aid: ``POST /api/v1/ontology/suggest`` infers a typed
ontology from a text sample (schema synthesis) and returns it in the SAME shape the per-graph
ontology PUT accepts, so a client can review and then save it onto a graph.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from oraclous_governance.propagation import OrganisationContext
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.dependencies import (
    OntologyServiceDep,
    UserIdDep,
    bind_org_context,
)
from oraclous_knowledge_graph_service.schema.ontology_schemas import (
    OntologyRequest,
    OntologyResponse,
    SuggestedOntologyResponse,
    SuggestOntologyRequest,
)
from oraclous_knowledge_graph_service.services.credential_client import make_credential_broker
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound
from oraclous_knowledge_graph_service.services.model_credential import (
    ModelCredentialUnavailable,
    credential_for_graph,
)
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
    body: SuggestOntologyRequest,
    _user_id: UserIdDep,
    _org: Annotated[OrganisationContext, Depends(bind_org_context)],
) -> SuggestedOntologyResponse:
    """Infer a typed ontology from a text sample (schema synthesis). 503 when no LLM is configured.

    The synthesizer is built inside the handler (not as a dependency) so its fail-closed
    ``SchemaSynthesisUnavailable`` maps cleanly to 503 instead of escaping dependency resolution.
    """
    settings = get_settings()
    try:
        # #724: schema synthesis reads the caller's sample text, so it runs on the ORG's
        # credential. This route is deliberately graph-free (/api/v1/ontology/suggest infers an
        # ontology BEFORE a graph exists), so there is no pin to apply and the org default is the
        # only tier. A missing credential surfaces as the same 503 as a missing LLM, since from
        # the caller's side both mean "synthesis is not available to you right now".
        credential = await credential_for_graph(
            settings,
            organisation_id=uuid.UUID(enforced_organisation_id()),
            graph_id=None,
            graph_credential_id=None,
            broker_factory=make_credential_broker,
        )
        synthesizer = make_synthesizer(settings, credential=credential)
    except ModelCredentialUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except SchemaSynthesisUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    suggestion = await synthesizer.suggest(sample=body.sample, mode=body.mode)
    return SuggestedOntologyResponse(**suggestion)


@router.get("", response_model=OntologyResponse)
async def get_ontology(
    graph_id: uuid.UUID, service: OntologyServiceDep, _user_id: UserIdDep
) -> OntologyResponse:
    try:
        data = await service.get(graph_id=graph_id)
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
