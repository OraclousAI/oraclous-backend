"""Engine schedule routes (routes layer) — parse → ONE service call → HTTP map.

Cron schedules are fired by Celery Beat (``fire_due``); the API only registers/lists/deletes them.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_execution_engine_service.core.dependencies import PrincipalDep, ScheduleServiceDep
from oraclous_execution_engine_service.schema.engine_schemas import (
    AdoptedToolRunListResponse,
    AdoptedToolRunOut,
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
            target_kind=body.target_kind.value,
            manifest_inline=body.manifest,
            manifest_ref=body.manifest_ref,
            input_text=body.input,
            cron=body.cron,
            instance_id=body.instance_id,
            input_data=body.input_data,
        )
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ScheduleOut.model_validate(row)


@router.post(
    "/schedules/{schedule_id}/fire-now",
    response_model=ScheduleOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fire_schedule_now(
    schedule_id: uuid.UUID, principal: PrincipalDep, service: ScheduleServiceDep
) -> ScheduleOut:
    """Fire a schedule's CURRENT window now (#489) — reachable through the gateway so the deployed
    proof can fire without waiting for a real Beat tick. Reuses the Beat fire path, so a second
    same-window call is a no-op (the dedupe row blocks the second dispatch). 202 + the schedule."""
    try:
        row = await service.fire_now(schedule_id, principal)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ScheduleOut.model_validate(row)


@router.get("/schedules/{schedule_id}/runs", response_model=AdoptedToolRunListResponse)
async def list_schedule_runs(
    schedule_id: uuid.UUID, principal: PrincipalDep, service: ScheduleServiceDep
) -> AdoptedToolRunListResponse:
    """The adopted-tool-run rows a schedule produced (#489) — the readable, gateway-only proof a
    schedule fired + the stamped registry ``execution_id``(s) (the deployed e2e reads these, then
    fetches each registry execution by id). Org-scoped: another tenant's schedule yields nothing."""
    rows = await service.list_adopted_runs(schedule_id, principal)
    out = [AdoptedToolRunOut.model_validate(r) for r in rows]
    return AdoptedToolRunListResponse(runs=out, total=len(out))


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
