"""Engine schedule routes (ORAA-4 §21 routes layer) — parse → ONE service call → HTTP map.

Cron schedules are fired by Celery Beat (``fire_due``); the API only registers/lists/deletes them.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_execution_engine_service.core.dependencies import PrincipalDep, ScheduleServiceDep
from oraclous_execution_engine_service.schema.engine_schemas import (
    RegisterScheduleRequest,
    ScheduleListResponse,
    ScheduleOut,
)
from oraclous_execution_engine_service.services.schedule_service import ScheduleError

router = APIRouter(prefix="/v1/engine", tags=["engine-schedules"])


@router.post("/schedules", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def register_schedule(
    body: RegisterScheduleRequest, principal: PrincipalDep, service: ScheduleServiceDep
) -> ScheduleOut:
    try:
        row = await service.register(
            principal,
            type=body.type.value,
            manifest_inline=body.manifest,
            manifest_ref=body.manifest_ref,
            input_text=body.input,
            cron=body.cron,
        )
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ScheduleOut.model_validate(row)


@router.get("/schedules", response_model=ScheduleListResponse)
async def list_schedules(
    principal: PrincipalDep, service: ScheduleServiceDep
) -> ScheduleListResponse:
    rows = await service.list_schedules(principal)
    out = [ScheduleOut.model_validate(r) for r in rows]
    return ScheduleListResponse(schedules=out, total=len(out))


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: uuid.UUID, principal: PrincipalDep, service: ScheduleServiceDep
) -> None:
    try:
        await service.delete(schedule_id, principal)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
