"""Engine task-board routes (ORAA-4 §21 routes layer) — parse → ONE service call → HTTP map.

``GET /v1/engine/tasks`` is the human task board (the org's ESCALATED jobs); ``POST
/v1/engine/tasks/{job_id}/complete`` submits the human's output, which completes the harness
assignment and flips both the parked run and the engine job to SUCCEEDED.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_execution_engine_service.core.dependencies import PrincipalDep, TaskServiceDep
from oraclous_execution_engine_service.schema.engine_schemas import (
    ApproveTaskRequest,
    CompleteTaskRequest,
    JobOut,
    TaskListResponse,
)
from oraclous_execution_engine_service.services.task_service import TaskError

router = APIRouter(prefix="/v1/engine", tags=["engine-tasks"])


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(principal: PrincipalDep, service: TaskServiceDep) -> TaskListResponse:
    try:
        rows = await service.list_tasks(principal)
    except TaskError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    out = [JobOut.model_validate(r) for r in rows]
    return TaskListResponse(tasks=out, total=len(out))


@router.post("/tasks/{job_id}/complete", response_model=JobOut)
async def complete_task(
    job_id: uuid.UUID,
    body: CompleteTaskRequest,
    principal: PrincipalDep,
    service: TaskServiceDep,
) -> JobOut:
    try:
        job = await service.complete(job_id, principal, body.output)
    except TaskError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return JobOut.model_validate(job)


@router.post("/tasks/{job_id}/approve", response_model=JobOut)
async def approve_task(
    job_id: uuid.UUID,
    body: ApproveTaskRequest,
    principal: PrincipalDep,
    service: TaskServiceDep,
) -> JobOut:
    """Resolve a mid-loop HITL approval task: APPROVED resumes the harness loop (the gated tool
    runs), DENIED terminates it FAILED. (Entrypoint human tasks use /complete instead.)"""
    try:
        job = await service.approve(job_id, principal, body.decision, body.decision_reason)
    except TaskError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return JobOut.model_validate(job)
