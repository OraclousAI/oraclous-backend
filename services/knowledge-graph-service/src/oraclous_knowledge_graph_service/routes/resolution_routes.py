"""Entity-resolution HITL routes (ORAA-4 §21 routes layer).

The action surface for the duplicate-candidate review queue (#279). Thin handlers: parse, ONE
service call, map a domain error to HTTP. No business logic, no DB access, no non-BaseModel classes.
Mounted under the existing knowledge-graph prefix so the gateway routes it to this writer
(`/api/v1/graphs` → KNOWLEDGE_GRAPH_URL); the read-only retriever cannot mutate.

  POST /api/v1/graphs/{graph_id}/resolution/{candidate_id}/approve  → merge the pair (survivor).
  POST /api/v1/graphs/{graph_id}/resolution/{candidate_id}/reject   → suppress + drop the edge.

`candidate_id` is the stable, unordered pair id (sha256 of the sorted endpoint node-id pair); the
body carries the two node ids. The handler verifies the path id is the canonical hash of the body
pair, so the URL and the operands cannot disagree.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_graph_service.core.dependencies import ResolutionServiceDep, UserIdDep
from oraclous_knowledge_graph_service.schema.resolution_schemas import (
    ApproveResponse,
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
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    except CandidateNotFound:
        raise _CANDIDATE_NOT_FOUND from None
    except ResolutionConflict:
        raise _conflict() from None
    return RejectResponse.of(candidate_id, outcome)
