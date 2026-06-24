"""Search routes (routes layer) — semantic / fulltext / hybrid over the graph KGS wrote.

Thin: parse → one service call → return list[NodeResult]. Org scope is bound by the dependency
chain behind the retrieval service; graph_id is a request field.
"""

from __future__ import annotations

from fastapi import APIRouter

from oraclous_knowledge_retriever_service.core.dependencies import RetrievalServiceDep, UserIdDep
from oraclous_knowledge_retriever_service.schema.search_schemas import (
    NodeResultModel,
    SearchRequest,
)

router = APIRouter(prefix="/v1/search", tags=["search"])


def _precedence(body: SearchRequest) -> tuple[list[str] | None, bool]:
    """Unpack the optional Hierarchy-of-Truth ordering (#514) for the service call."""
    p = body.precedence
    return (p.order if p else None, p.graph_authoritative if p else False)


@router.post("/semantic", response_model=list[NodeResultModel])
async def semantic_search(
    body: SearchRequest, service: RetrievalServiceDep, _user_id: UserIdDep
) -> list[NodeResultModel]:
    order, graph_authoritative = _precedence(body)
    results = await service.semantic(
        graph_id=str(body.graph_id),
        query=body.query,
        top_k=body.top_k,
        precedence_order=order,
        graph_authoritative=graph_authoritative,
    )
    return [NodeResultModel(**r) for r in results]


@router.post("/fulltext", response_model=list[NodeResultModel])
async def fulltext_search(
    body: SearchRequest, service: RetrievalServiceDep, _user_id: UserIdDep
) -> list[NodeResultModel]:
    order, graph_authoritative = _precedence(body)
    results = await service.fulltext(
        graph_id=str(body.graph_id),
        query=body.query,
        top_k=body.top_k,
        precedence_order=order,
        graph_authoritative=graph_authoritative,
    )
    return [NodeResultModel(**r) for r in results]


@router.post("/hybrid", response_model=list[NodeResultModel])
async def hybrid_search(
    body: SearchRequest, service: RetrievalServiceDep, _user_id: UserIdDep
) -> list[NodeResultModel]:
    order, graph_authoritative = _precedence(body)
    results = await service.hybrid(
        graph_id=str(body.graph_id),
        query=body.query,
        top_k=body.top_k,
        precedence_order=order,
        graph_authoritative=graph_authoritative,
    )
    return [NodeResultModel(**r) for r in results]
