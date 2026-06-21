"""Engine job routes (routes layer) — parse → ONE service/repo call → HTTP map. No logic.

``POST .../jobs`` submits a durable harness job (S1: runs synchronously, returns the terminal job);
``GET .../jobs/{id}`` + ``GET .../jobs`` read prior jobs (org-scoped). A bad request body is 400; an
auth/scope failure is 401; the store being down is 503.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_execution_engine_service.core.dependencies import (
    JobServiceDep,
    PrincipalDep,
)
from oraclous_execution_engine_service.schema.engine_schemas import (
    EngineEventRequest,
    EngineEventResponse,
    JobListResponse,
    JobOut,
    SubmitJobRequest,
)
from oraclous_execution_engine_service.services.job_service import JobError

router = APIRouter(prefix="/v1/engine", tags=["engine"])


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(
    body: SubmitJobRequest, principal: PrincipalDep, service: JobServiceDep
) -> JobOut:
    """Accept a durable job (202) — it runs on the worker. Poll GET /jobs/{id} for the outcome."""
    try:
        job = await service.submit(
            principal=principal,
            input_text=body.input,
            manifest_inline=body.manifest,
            manifest_ref=body.manifest_ref,
            max_retries=body.max_retries,
            timeout_seconds=body.timeout_seconds,
        )
    except JobError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return JobOut.model_validate(job)


@router.post("/events", response_model=EngineEventResponse, status_code=status.HTTP_202_ACCEPTED)
async def fire_event(
    body: EngineEventRequest, principal: PrincipalDep, service: JobServiceDep
) -> EngineEventResponse:
    """Fire a webhook EVENT -> a durable job (202). The trusted gateway (X-Internal-Key + the
    X-Principal-*/X-Organisation-Id, the same gate as /jobs) calls this AFTER it has verified the
    inbound signature; the org is the gateway-asserted principal's, never this body. Idempotent on
    the delivery key — a re-delivered event is a no-op (``deduped: true``), still 202."""
    try:
        job = await service.submit_event(
            principal=principal,
            input_text=body.input,
            idempotency_key=body.idempotency_key,
            manifest_inline=body.manifest,
            manifest_ref=body.manifest_ref,
        )
    except JobError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return EngineEventResponse(deduped=job is None, job_id=job.id if job is not None else None)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: uuid.UUID, principal: PrincipalDep, service: JobServiceDep) -> JobOut:
    """Cancel a QUEUED/RUNNING/ESCALATED job (a terminal job is returned unchanged)."""
    try:
        job = await service.cancel(job_id, principal)
    except JobError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return JobOut.model_validate(job)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(principal: PrincipalDep, service: JobServiceDep) -> JobListResponse:
    rows = await service.list(principal)
    out = [JobOut.model_validate(r) for r in rows]
    return JobListResponse(jobs=out, total=len(out))


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, principal: PrincipalDep, service: JobServiceDep) -> JobOut:
    row = await service.get(job_id, principal)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return JobOut.model_validate(row)
