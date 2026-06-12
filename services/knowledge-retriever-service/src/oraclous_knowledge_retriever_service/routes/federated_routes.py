"""Federated cross-graph routes (ORAA-4 §21 routes layer) — #330 / ADR-026.

POST /v1/federated/search    — entity / semantic / fulltext / hybrid across the caller's
                               accessible graphs; every hit labeled source_graph_id/name.
POST /v1/federated/subgraph  — the neighborhood slice around matched entities, per graph.

Thin: parse → one service call → map domain errors to HTTP. Error map: an inaccessible/unknown id
in an explicit subset → 403 (fail-closed, no partial results, no existence oracle); a cap breach →
422; an un-enumerable accessible set (registry down/unconfigured) → 503 (never "assume all").
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_retriever_service.core.dependencies import (
    FederatedServiceDep,
    PrincipalDep,
)
from oraclous_knowledge_retriever_service.schema.federated_schemas import (
    FederatedSearchRequest,
    FederatedSearchResponse,
    FederatedSubgraphRequest,
    FederatedSubgraphResponse,
)
from oraclous_knowledge_retriever_service.services.federated_service import (
    FederatedAccessError,
    FederatedCapError,
)
from oraclous_knowledge_retriever_service.services.graph_registry_client import GraphRegistryError

router = APIRouter(prefix="/v1/federated", tags=["federated"])


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FederatedAccessError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, FederatedCapError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="the graph registry is unavailable; the accessible set cannot be enumerated",
    )


@router.post("/search", response_model=FederatedSearchResponse)
async def federated_search(
    body: FederatedSearchRequest, service: FederatedServiceDep, principal: PrincipalDep
) -> FederatedSearchResponse:
    try:
        data = await service.search(
            principal=principal,
            query=body.query,
            mode=body.mode,
            graph_ids=body.graph_ids,
            per_graph_k=body.per_graph_k,
            total_k=body.total_k,
        )
    except (FederatedAccessError, FederatedCapError, GraphRegistryError) as exc:
        raise _map_error(exc) from None
    return FederatedSearchResponse(
        results=data["results"], total=len(data["results"]), meta=data["meta"]
    )


@router.post("/subgraph", response_model=FederatedSubgraphResponse)
async def federated_subgraph(
    body: FederatedSubgraphRequest, service: FederatedServiceDep, principal: PrincipalDep
) -> FederatedSubgraphResponse:
    try:
        data = await service.neighborhood(
            principal=principal,
            query=body.query,
            graph_ids=body.graph_ids,
            entities_per_graph=body.entities_per_graph,
            limit_per_graph=body.limit_per_graph,
        )
    except (FederatedAccessError, FederatedCapError, GraphRegistryError) as exc:
        raise _map_error(exc) from None
    return FederatedSubgraphResponse(nodes=data["nodes"], edges=data["edges"], meta=data["meta"])
