"""Ingestion routes (ORAA-4 §21 routes layer) — thin: parse → one service call → HTTP map.

POST /ingest (inline text) and /upload (file) create a job and enqueue async processing (202).
GET /jobs/{job_id} polls status; GET /documents lists ingested documents (the job rows) for a graph.
Owner/org scoping is enforced in the service/repository; here we only map domain errors to HTTP.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from oraclous_knowledge_graph_service.core.dependencies import JobServiceDep, UserIdDep
from oraclous_knowledge_graph_service.schema.ingest_schemas import IngestTextRequest, JobResponse
from oraclous_knowledge_graph_service.services.extractors import ExtractionError, source_type_for
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound
from oraclous_knowledge_graph_service.services.job_service import JobNotFound

router = APIRouter(prefix="/api/v1/graphs/{graph_id}", tags=["ingestion"])

_GRAPH_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")


@router.post("/ingest", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_text(
    graph_id: uuid.UUID, body: IngestTextRequest, service: JobServiceDep, user_id: UserIdDep
) -> JobResponse:
    default_name = "inline.txt" if body.source_type == "text" else f"inline.{body.source_type}"
    try:
        job = await service.submit(
            user_id=user_id,
            graph_id=graph_id,
            data=body.content.encode("utf-8"),
            filename=body.filename or default_name,
            source_type=body.source_type,
            recipe_id=body.recipe_id,
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    return JobResponse.of(job)


@router.post("/upload", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    graph_id: uuid.UUID,
    service: JobServiceDep,
    user_id: UserIdDep,
    file: UploadFile = File(...),  # noqa: B008 — FastAPI File() marker is the idiom
    recipe_id: str | None = Form(default=None),  # noqa: B008 — FastAPI Form() marker is the idiom
) -> JobResponse:
    try:
        source_type = source_type_for(file.filename)
    except ExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    data = await file.read()
    try:
        job = await service.submit(
            user_id=user_id,
            graph_id=graph_id,
            data=data,
            filename=file.filename,
            source_type=source_type,
            recipe_id=recipe_id,
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    return JobResponse.of(job)


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    graph_id: uuid.UUID, job_id: uuid.UUID, service: JobServiceDep, user_id: UserIdDep
) -> JobResponse:
    try:
        job = await service.get_job(user_id=user_id, graph_id=graph_id, job_id=job_id)
    except (GraphNotFound, JobNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found") from None
    return JobResponse.of(job)


@router.get("/documents", response_model=list[JobResponse])
async def list_documents(
    graph_id: uuid.UUID, service: JobServiceDep, user_id: UserIdDep
) -> list[JobResponse]:
    try:
        jobs = await service.list_documents(user_id=user_id, graph_id=graph_id)
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    return [JobResponse.of(j) for j in jobs]
