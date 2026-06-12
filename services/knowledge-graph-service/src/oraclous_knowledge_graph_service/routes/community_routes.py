"""Community + analytics routes (ORAA-4 §21 routes layer) — thin: parse → one service call → map.

Restores the legacy community/analytics surface (``communities.py`` + the ``graphs.py`` community
routes), RE-ARCHITECTED onto the in-DB GDS analytics service. No business logic, no DB access, no
non-BaseModel classes here. Mounted under the knowledge-graph prefix so the gateway routes it to
this writer.

  GET  /api/v1/communities/kinds                                  → the kind registry
  GET  /api/v1/graphs/{graph_id}/communities?level=&kind=         → list communities
  POST /api/v1/graphs/{graph_id}/communities/detect               → detect (202 async / 200 sync)
  GET  /api/v1/graphs/{graph_id}/communities/status               → detection status
  GET  /api/v1/graphs/{graph_id}/communities/{community_id}       → one community + members
  POST /api/v1/graphs/{graph_id}/communities/summarize?level=     → LLM-summarise communities
  GET  /api/v1/graphs/{graph_id}/analytics                        → graph statistics
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from oraclous_knowledge_graph_service.core.dependencies import AnalyticsServiceDep, UserIdDep
from oraclous_knowledge_graph_service.domain.community import GdsUnavailableError
from oraclous_knowledge_graph_service.schema.community_schemas import (
    AnalyticsResponse,
    CommunitiesStatusResponse,
    CommunityKindResponse,
    CommunityResponse,
    DetectAcceptedResponse,
    DetectionResultResponse,
    DetectRequest,
    SummarizeResponse,
)
from oraclous_knowledge_graph_service.services.analytics_service import (
    SummarizationUnavailable,
    UnknownCommunityKind,
)
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound

# Two routers: the kind registry is graph-independent (legacy mounted it without a graph_id); the
# rest hang off a graph.
kinds_router = APIRouter(prefix="/api/v1/communities", tags=["communities"])
router = APIRouter(prefix="/api/v1/graphs/{graph_id}", tags=["communities"])

_GRAPH_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")
_COMMUNITY_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="community not found"
)


def _gds_unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


@kinds_router.get("/kinds", response_model=list[CommunityKindResponse])
async def list_kinds(
    _user_id: UserIdDep, service: AnalyticsServiceDep
) -> list[CommunityKindResponse]:
    return [CommunityKindResponse.of(k) for k in service.kinds()]


@router.get("/communities", response_model=list[CommunityResponse])
async def list_communities(
    graph_id: uuid.UUID,
    service: AnalyticsServiceDep,
    user_id: UserIdDep,
    level: int | None = None,
    kind: str = "entity",
) -> list[CommunityResponse]:
    try:
        communities = await service.list_communities(
            graph_id=graph_id, user_id=user_id, level=level, kind=kind
        )
    except UnknownCommunityKind as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown community kind: {exc}"
        ) from None
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    return [CommunityResponse.of(c) for c in communities]


@router.post("/communities/detect")
async def detect_communities(
    graph_id: uuid.UUID,
    body: DetectRequest,
    service: AnalyticsServiceDep,
    user_id: UserIdDep,
    response: Response,
) -> DetectAcceptedResponse | DetectionResultResponse:
    """Detect communities. The status code carries the sync/async signal: a tiny graph runs INLINE
    and returns the result with **200 OK**; a larger graph enqueues a job and returns
    ``{job_id,status}`` with **202 Accepted**."""
    try:
        job, result = await service.submit_detect(
            graph_id=graph_id,
            user_id=user_id,
            min_entities=body.min_entities,
            force_rebuild=body.force_rebuild,
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    except GdsUnavailableError as exc:
        raise _gds_unavailable(str(exc)) from None
    if job is not None:
        response.status_code = status.HTTP_202_ACCEPTED
        return DetectAcceptedResponse(job_id=str(job.id), status=job.status)
    response.status_code = status.HTTP_200_OK
    return DetectionResultResponse.of(result)  # type: ignore[arg-type]


@router.get("/communities/status", response_model=CommunitiesStatusResponse)
async def communities_status(
    graph_id: uuid.UUID, service: AnalyticsServiceDep, user_id: UserIdDep
) -> CommunitiesStatusResponse:
    try:
        result = await service.status(graph_id=graph_id, user_id=user_id)
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    return CommunitiesStatusResponse.of(result)


@router.get("/communities/{community_id}", response_model=CommunityResponse)
async def get_community(
    graph_id: uuid.UUID,
    community_id: str,
    service: AnalyticsServiceDep,
    user_id: UserIdDep,
) -> CommunityResponse:
    try:
        community = await service.get_community(
            graph_id=graph_id, user_id=user_id, community_id=community_id
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    if community is None:
        raise _COMMUNITY_NOT_FOUND
    return CommunityResponse.of(community)


@router.post("/communities/summarize", response_model=SummarizeResponse)
async def summarize_communities(
    graph_id: uuid.UUID,
    service: AnalyticsServiceDep,
    user_id: UserIdDep,
    level: int | None = None,
    force: bool = False,
) -> SummarizeResponse:
    """LLM-summarise communities. By default only un-summarised ones (so a re-run resumes without
    re-billing); ``force=true`` re-summarises all. A run whose candidate count exceeds the inline
    cap returns ``status="deferred"`` (not a silent ``summarized=0``) so the caller can route async.
    """
    try:
        outcome = await service.summarize(
            graph_id=graph_id, user_id=user_id, level=level, force=force
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    except SummarizationUnavailable as exc:
        raise _gds_unavailable(str(exc)) from None
    return SummarizeResponse(
        graph_id=str(graph_id),
        summarized=len(outcome.results),
        status=outcome.status,
        deferred=outcome.deferred_count,
    )


@router.get("/analytics", response_model=AnalyticsResponse)
async def graph_analytics(
    graph_id: uuid.UUID, service: AnalyticsServiceDep, user_id: UserIdDep
) -> AnalyticsResponse:
    try:
        result = await service.analytics(graph_id=graph_id, user_id=user_id)
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    return AnalyticsResponse.of(result)
