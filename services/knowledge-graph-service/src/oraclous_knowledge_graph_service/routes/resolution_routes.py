"""Entity-resolution HITL routes (routes layer).

The action surface for the duplicate-candidate review queue (#279). Thin handlers: parse, ONE
service call, map a domain error to HTTP. No business logic, no DB access, no non-BaseModel classes.
Mounted under the existing knowledge-graph prefix so the gateway routes it to this writer
(`/api/v1/graphs` → KNOWLEDGE_GRAPH_URL); the read-only retriever cannot mutate.

  POST /api/v1/graphs/{graph_id}/resolution/{candidate_id}/approve  → merge the pair (survivor).
  POST /api/v1/graphs/{graph_id}/resolution/{candidate_id}/reject   → suppress + drop the edge.
  POST /api/v1/graphs/{graph_id}/resolution/cross-graph             → generate cross-graph
       SAME_AS candidates between the path graph and a second org-owned graph (#330 / ADR-026);
       the pairs land in the SAME review queue and the SAME verdict endpoints action them (the
       body's `other_graph_id` marks the cross-graph case; an approve LINKS instead of folding).

`candidate_id` is the stable, unordered pair id (sha256 of the sorted endpoint node-id pair); the
body carries the two node ids. The handler verifies the path id is the canonical hash of the body
pair, so the URL and the operands cannot disagree.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from oraclous_knowledge_graph_service.core.dependencies import ResolutionServiceDep, UserIdDep
from oraclous_knowledge_graph_service.schema.resolution_schemas import (
    ApproveResponse,
    CrossGraphCandidateModel,
    CrossGraphGenerateRequest,
    CrossGraphGenerateResponse,
    PendingCrossGraphResponse,
    RejectResponse,
    ResolveCandidateRequest,
)
from oraclous_knowledge_graph_service.services.resolution_service import (
    CandidateNotFound,
    GraphNotFound,
    ResolutionConflict,
)

router = APIRouter(prefix="/api/v1/graphs", tags=["resolution"])

_GRAPH_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")
_CANDIDATE_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="candidate pair not found"
)


def _conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="candidate already resolved with a different verdict by another reviewer",
    )


@router.post("/{graph_id}/resolution/{candidate_id}/approve", response_model=ApproveResponse)
async def approve_candidate(
    graph_id: uuid.UUID,
    candidate_id: str,
    body: ResolveCandidateRequest,
    service: ResolutionServiceDep,
    user_id: UserIdDep,
) -> ApproveResponse:
    try:
        pair = body.to_pair()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    try:
        outcome = await service.approve(
            graph_id=graph_id,
            user_id=user_id,
            pair=pair,
            candidate_id_path=candidate_id,
            other_graph_id=body.other_graph_id,
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    except CandidateNotFound:
        raise _CANDIDATE_NOT_FOUND from None
    except ResolutionConflict:
        raise _conflict() from None
    return ApproveResponse.of(candidate_id, outcome)


@router.post("/{graph_id}/resolution/{candidate_id}/reject", response_model=RejectResponse)
async def reject_candidate(
    graph_id: uuid.UUID,
    candidate_id: str,
    body: ResolveCandidateRequest,
    service: ResolutionServiceDep,
    user_id: UserIdDep,
) -> RejectResponse:
    try:
        pair = body.to_pair()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    try:
        outcome = await service.reject(
            graph_id=graph_id,
            user_id=user_id,
            pair=pair,
            candidate_id_path=candidate_id,
            other_graph_id=body.other_graph_id,
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    except CandidateNotFound:
        raise _CANDIDATE_NOT_FOUND from None
    except ResolutionConflict:
        raise _conflict() from None
    return RejectResponse.of(candidate_id, outcome)


@router.post("/{graph_id}/resolution/cross-graph", response_model=CrossGraphGenerateResponse)
async def generate_cross_graph_candidates(
    graph_id: uuid.UUID,
    body: CrossGraphGenerateRequest,
    service: ResolutionServiceDep,
    user_id: UserIdDep,
) -> CrossGraphGenerateResponse:
    """Generate cross-graph SAME_AS candidates between the path graph and `target_graph_id`
    (#330). Both graphs must be the caller's (a graph in another org/owner → 404, fail-closed —
    a cross-ORG candidate pair is impossible). The flagged pairs are written as
    `SAME_AS_CANDIDATE` edges carrying BOTH graph ids; the response is the review queue."""
    try:
        candidates, warnings = await service.generate_cross_graph(
            graph_id=graph_id,
            target_graph_id=body.target_graph_id,
            user_id=user_id,
            candidate_threshold=body.candidate_threshold,
            limit=body.limit,
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return CrossGraphGenerateResponse(
        candidates=[CrossGraphCandidateModel.of(c) for c in candidates],
        generated=len(candidates),
        warnings=warnings,
    )


@router.get("/{graph_id}/resolution/cross-graph", response_model=PendingCrossGraphResponse)
async def list_pending_cross_graph_candidates(
    graph_id: uuid.UUID,
    service: ResolutionServiceDep,
    user_id: UserIdDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> PendingCrossGraphResponse:
    """The pending cross-graph SAME_AS review queue touching this graph (#330) — what a HITL
    reviewer reads to see the candidates a prior generation run wrote (the generation response is
    otherwise the only place they surface). Owner-gated (a graph not in the caller's org/owner →
    404). Each pending pair keys the same approve/reject verdict endpoints."""
    try:
        candidates = await service.list_pending_cross_graph(
            graph_id=graph_id, user_id=user_id, limit=limit
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    return PendingCrossGraphResponse(
        candidates=[CrossGraphCandidateModel.of(c) for c in candidates],
        total=len(candidates),
    )
