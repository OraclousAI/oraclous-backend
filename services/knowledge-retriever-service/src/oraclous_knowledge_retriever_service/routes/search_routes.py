"""Search routes (routes layer) — semantic / fulltext / hybrid over the graph KGS wrote.

Thin: parse → one service call → return list[NodeResult]. Org scope is bound by the dependency
chain behind the retrieval service; graph_id is a request field.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_retriever_service.core.dependencies import (
    MODEL_CREDENTIAL_REJECTED_DETAIL,
    MODEL_CREDENTIAL_REQUIRED_DETAIL,
    RetrievalServiceDep,
    UserIdDep,
)
from oraclous_knowledge_retriever_service.schema.search_schemas import (
    NodeResultModel,
    SearchRequest,
)
from oraclous_knowledge_retriever_service.services.retrieval_service import (
    IDENTITY_MISMATCH_DETAIL,
    EmbedderIdentityMismatch,
    QueryEmbeddingCredentialExhausted,
    QueryEmbeddingCredentialRejected,
    QueryEmbeddingUnavailable,
)

router = APIRouter(prefix="/v1/search", tags=["search"])


# #949 Q3: the graph holds chunks, but none in the space this deployment's query embedder produces
# — mid re-embed, or written by a different model. 409, because nothing about the REQUEST is wrong;
# it is the graph's current state that cannot serve it, and it becomes servable on its own once the
# re-embed pass finishes. The sentence itself lives beside the exception in the services layer, so
# the federated route renders the identical refusal rather than a second copy of the same copy.
def _identity_mismatch() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=IDENTITY_MISMATCH_DETAIL)


def _embedding_failed(exc: QueryEmbeddingUnavailable) -> HTTPException:
    """The credential resolved, then the provider itself failed the embed call.

    Three answers, because the caller's next move differs for each (#1109 ruling 3):

      * REFUSED credential (401/403) -> 422 MODEL_CREDENTIAL_REJECTED. The organisation stored a
        credential and designated it; it has to be REPLACED. This used to reuse
        MODEL_CREDENTIAL_REQUIRED purely to avoid minting a code the closed taxonomy and the
        gateway's relay allow-list did not carry — both carry this one now, and REQUIRED's advice
        ("store one and designate it") is simply wrong for a key that is already stored.
      * EXHAUSTED credential (429/quota) -> 422 MODEL_CREDENTIAL_REQUIRED, unchanged. The key is
        good but out of quota; a code of its own is a separate decision, not this one.
      * anything else -> 503. The provider was unreachable: the platform's problem, not the
        caller's, so a retry rather than a bare 500 with the provider's own text in it.

    None of the three relays the provider's message — it can name internal hosts (rule 8); each
    detail is a curated line.
    """
    if isinstance(exc, QueryEmbeddingCredentialRejected):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=MODEL_CREDENTIAL_REJECTED_DETAIL,
        )
    if isinstance(exc, QueryEmbeddingCredentialExhausted):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=MODEL_CREDENTIAL_REQUIRED_DETAIL,
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "meaning-based search is temporarily unavailable: the embedding provider could not be"
            " reached. Try again shortly."
        ),
    )


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
    except QueryEmbeddingUnavailable as exc:
        raise _embedding_failed(exc) from None
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
    except QueryEmbeddingUnavailable as exc:
        raise _embedding_failed(exc) from None
    return [NodeResultModel(**r) for r in results]
