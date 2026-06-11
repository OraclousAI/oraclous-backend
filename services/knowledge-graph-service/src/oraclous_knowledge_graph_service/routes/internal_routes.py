"""Internal routes (ORAA-4 §21 routes layer) — the agent/capability-addressable, gateway-trusted
surface (ADR-018).

These endpoints are reached over the internal trust path: in gateway mode the caller's verified
identity arrives as X-Principal-*/X-Organisation-Id, gated by X-Internal-Key (enforced in
``get_principal`` via the ``UserIdDep`` dependency — a missing/wrong key is 403, no token → 401).
The org is bound from the FORWARDED principal (never the body), so every read/write is scoped to the
caller's tenant.

  GET  /internal/v1/schema/{graph_id} — the org-scoped label/relationship shape of a graph.
  POST /internal/v1/ingest            — enqueue ingestion into an org-owned graph (Slice C), the
                                        write twin of the internal SEARCH the retriever calls. It
                                        REUSES the user-facing ingestion service/task verbatim;
                                        org-ownership of the target graph is enforced by the same
                                        owner gate (a graph not in the principal's org → 404).
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_graph_service.core.dependencies import (
    GraphWriteRepoDep,
    JobServiceDep,
    UserIdDep,
)
from oraclous_knowledge_graph_service.schema.ingest_schemas import (
    InternalIngestRequest,
    JobResponse,
    LabelCount,
    RelTypeCount,
    SchemaResponse,
)
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound

router = APIRouter(prefix="/internal/v1", tags=["internal"])


@router.get("/schema/{graph_id}", response_model=SchemaResponse)
async def get_graph_schema(
    graph_id: uuid.UUID, repo: GraphWriteRepoDep, _user_id: UserIdDep
) -> SchemaResponse:
    from oraclous_substrate.access import enforced_organisation_id

    organisation_id = enforced_organisation_id()
    data = await asyncio.to_thread(
        repo.schema, graph_id=str(graph_id), organisation_id=organisation_id
    )
    return SchemaResponse(
        graph_id=graph_id,
        labels=[LabelCount(label=row["label"], count=row["count"]) for row in data["labels"]],
        relationships=[
            RelTypeCount(type=row["type"], count=row["count"]) for row in data["relationships"]
        ],
    )


@router.post("/ingest", response_model=JobResponse, status_code=202)
async def internal_ingest(
    body: InternalIngestRequest, service: JobServiceDep, user_id: UserIdDep
) -> JobResponse:
    """Enqueue ingestion into ``body.graph_id`` for the FORWARDED principal's org.

    Delegates to the SAME ``JobService.submit`` the user-facing ``POST /ingest`` uses (one job row +
    one enqueue), so the pipeline is not duplicated. The owner gate inside ``submit`` is org-scoped:
    a graph that is not in the principal's org raises ``GraphNotFound`` → 404, so a forwarded
    principal can never write into another tenant's graph.
    """
    default_name = "inline.txt" if body.source_type == "text" else f"inline.{body.source_type}"
    try:
        job = await service.submit(
            user_id=user_id,
            graph_id=body.graph_id,
            data=body.content.encode("utf-8"),
            filename=default_name,
            source_type=body.source_type,
            recipe_id=body.recipe_id,
        )
    except GraphNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="graph not found"
        ) from None
    return JobResponse.of(job)
