"""Search routes (routes layer) — semantic / fulltext / hybrid over the graph KGS wrote.

Thin: parse → one service call → return list[NodeResult]. Org scope is bound by the dependency
chain behind the retrieval service; graph_id is a request field.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_retriever_service.core.dependencies import RetrievalServiceDep, UserIdDep
from oraclous_knowledge_retriever_service.schema.search_schemas import (
    NodeResultModel,
    SearchRequest,
)
from oraclous_knowledge_retriever_service.services.retrieval_service import (
    EmbedderIdentityMismatch,
)

router = APIRouter(prefix="/v1/search", tags=["search"])

# #949 Q3: the graph holds chunks, but none in the space this deployment's query embedder produces
# — mid re-embed, or written by a different model. 409, because nothing about the REQUEST is wrong;
# it is the graph's current state that cannot serve it, and it becomes servable on its own once the
# re-embed pass finishes. The message names no graph id or identity string: the gateway drains an
# upstream body anyway, and a curated line is what a retry would be based on.
_IDENTITY_MISMATCH = (
    "this graph's stored embeddings were produced by a different embedder than this search uses,"
    " so they cannot be compared; re-embedding is in progress — try again shortly."
)


def _identity_mismatch() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_IDENTITY_MISMATCH)


def _precedence(body: SearchRequest) -> tuple[list[str] | None, bool]:
    """Unpack the optional Hierarchy-of-Truth ordering (#514) for the service call."""
    p = body.precedence
    return (p.order if p else None, p.graph_authoritative if p else False)


@router.post("/semantic", response_model=list[NodeResultModel])
async def semantic_search(
    body: SearchRequest, service: RetrievalServiceDep, _user_id: UserIdDep
) -> list[NodeResultModel]:
    order, graph_authoritative = _precedence(body)
    try:
        results = await service.semantic(
            graph_id=str(body.graph_id),
            query=body.query,
            top_k=body.top_k,
            precedence_order=order,
            graph_authoritative=graph_authoritative,
        )
    except EmbedderIdentityMismatch:
        raise _identity_mismatch() from None
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
    try:
        results = await service.hybrid(
            graph_id=str(body.graph_id),
            query=body.query,
            top_k=body.top_k,
            precedence_order=order,
            graph_authoritative=graph_authoritative,
        )
    except EmbedderIdentityMismatch:
        raise _identity_mismatch() from None
    return [NodeResultModel(**r) for r in results]
