"""/v1/artifacts — the unified artifact read/serve surface (#543, ADR-041).

A team's outputs live on Oraclous (graph-indexed) and are served here through ONE endpoint (not a
per-artifact zoo): list a graph's artifacts (its ingested documents), optionally filtered by a
filename query or source_type, and fetch a single artifact's verbatim content. Org-scoped: the org
is bound from the principal via graph ownership (never a body field); ``organisation_id`` is never
exposed. Rich semantic query stays the retriever's ``/v1/search`` over the same graph.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_graph_service.core.dependencies import JobServiceDep, UserIdDep
from oraclous_knowledge_graph_service.schema.artifacts_schemas import (
    ArtifactDetail,
    ArtifactSummary,
)
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound
from oraclous_knowledge_graph_service.services.job_service import JobNotFound

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


@router.get("", response_model=list[ArtifactSummary])
async def list_artifacts(
    graph_id: uuid.UUID,
    service: JobServiceDep,
    user_id: UserIdDep,
    q: str | None = None,
    source_type: str | None = None,
) -> list[ArtifactSummary]:
    try:
        records = await service.list_artifacts(
            user_id=user_id, graph_id=graph_id, q=q, source_type=source_type
        )
    except GraphNotFound:
        raise _NOT_FOUND from None
    return [ArtifactSummary.of(r) for r in records]


@router.get("/{artifact_id}", response_model=ArtifactDetail)
async def get_artifact(
    artifact_id: uuid.UUID, service: JobServiceDep, user_id: UserIdDep
) -> ArtifactDetail:
    try:
        rec, content = await service.get_artifact(user_id=user_id, artifact_id=artifact_id)
    except (GraphNotFound, JobNotFound):
        raise _NOT_FOUND from None
    return ArtifactDetail.of_with_content(rec, content)
