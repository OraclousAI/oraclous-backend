"""Ingestion routes (routes layer) — thin: parse → one service call → HTTP map.

POST /ingest (inline text) and /upload (file) create a job and enqueue async processing (202).
GET /jobs/{job_id} polls status; GET /documents lists ingested documents (the job rows) for a graph.
Owner/org scoping is enforced in the service/repository; here we only map domain errors to HTTP.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from oraclous_knowledge_graph_service.core.dependencies import (
    GraphServiceDep,
    JobServiceDep,
    RecipeServiceDep,
    SqlIngestionServiceDep,
    UserIdDep,
)
from oraclous_knowledge_graph_service.domain.connectors.sql_connector import DbSyncMode
from oraclous_knowledge_graph_service.schema.ingest_schemas import (
    BatchIngestRequest,
    BatchIngestResponse,
    IngestTextRequest,
    JobResponse,
    SqlIngestRequest,
    SqlIngestResponse,
)
from oraclous_knowledge_graph_service.services.extractors import ExtractionError, source_type_for
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound
from oraclous_knowledge_graph_service.services.job_service import JobNotFound
from oraclous_knowledge_graph_service.services.sql_ingestion_service import SqlIngestionError

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
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            event_time=body.event_time,
        )
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    return JobResponse.of(job)


@router.post(
    "/batch-ingest", response_model=BatchIngestResponse, status_code=status.HTTP_202_ACCEPTED
)
async def batch_ingest(
    graph_id: uuid.UUID, body: BatchIngestRequest, service: JobServiceDep, user_id: UserIdDep
) -> BatchIngestResponse:
    """Land a FOLDER/REPO of content in the org graph in one call (#522, the cloud content-in flow):
    one async ingest job per item, each idempotent on its ``path`` (re-ingest replaces, never
    duplicates). Thin orchestration over single-ingest — poll each job via ``GET /jobs/{id}``.
    Org-scoped: the org is bound from the principal, never the body."""
    jobs: list[JobResponse] = []
    try:
        for item in body.items:
            job = await service.submit(
                user_id=user_id,
                graph_id=graph_id,
                data=item.content.encode("utf-8"),
                filename=item.path,
                source_type=item.source_type,
                recipe_id=item.recipe_id,
            )
            jobs.append(JobResponse.of(job))
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    return BatchIngestResponse(jobs=jobs)


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


@router.post("/ingest-sql", response_model=SqlIngestResponse)
async def ingest_sql(
    graph_id: uuid.UUID,
    body: SqlIngestRequest,
    sql_service: SqlIngestionServiceDep,
    graphs: GraphServiceDep,
    recipes: RecipeServiceDep,
    user_id: UserIdDep,
) -> SqlIngestResponse:
    """Relational (SQL) ingest (#307): resolve the connection_string by `credential_id`,
    egress-check the DB host, introspect, then project rows→entities + FK→relationships via the
    recipe engine.

    Synchronous (the recipe engine runs inline, like a `dry_run`): a SQL ingest is bounded by the
    introspected schema + the per-table row cap. The org+graph scope is server-injected (the owner
    gate below + the bound org); the caller never supplies the org.
    """
    # Owner gate: only the graph's owner (in the bound org) may ingest into it (404 otherwise).
    try:
        await graphs.get_graph(graph_id=graph_id, user_id=user_id)
    except GraphNotFound:
        raise _GRAPH_NOT_FOUND from None
    try:
        sync_mode = DbSyncMode(body.sync_mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported sync_mode {body.sync_mode!r} (full_snapshot|schema_only)",
        ) from exc
    # A stored recipe (by id) wins over the synthesised default-relational recipe.
    recipe = None
    if body.recipe_id:
        recipe = await recipes.get(body.recipe_id)
        if recipe is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"recipe {body.recipe_id!r} not found",
            )
        # Only a PROMOTED recipe is runnable — a draft must be promoted before it can ingest, so a
        # graph run always pins to a reviewed, immutable recipe version (ADR-028).
        if recipe.get("status") != "promoted":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"recipe {body.recipe_id!r} is a draft; promote it before ingesting",
            )
    try:
        result = await sql_service.ingest(
            graph_id=str(graph_id),
            credential_id=body.credential_id,
            sync_mode=sync_mode,
            schema=body.schema_name,
            recipe=recipe,
        )
    except SqlIngestionError as exc:
        # Credential / egress / connect / empty-schema failures are client-correctable inputs.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return SqlIngestResponse(
        graph_id=graph_id,
        dialect=result["dialect"],
        database=result["database"],
        schema_name=result["schema"],
        sync_mode=result["sync_mode"],
        tables_introspected=result["tables_introspected"],
        nodes_written=result["nodes_written"],
        edges_written=result["edges_written"],
        containers_written=result["containers_written"],
        properties_written=result["properties_written"],
        units_skipped=result["units_skipped"],
        warnings=result["warnings"],
    )


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
