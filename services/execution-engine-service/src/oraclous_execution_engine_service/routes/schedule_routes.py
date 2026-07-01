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
    MemberCostOut,
    PreflightCostResponse,
    PreflightScheduleRequest,
    RegisterScheduleRequest,
    ScheduledTeamRunListResponse,
    ScheduledTeamRunOut,
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
            graph_id=body.graph_id,  # #601: a team schedule's persistent graph workspace
            # #598: the L3 per-period cap (team-only; None/None => OFF). budget_period is the enum.
            budget_period=body.budget_period.value if body.budget_period else None,
            budget_allowance_tokens=body.budget_allowance_tokens,
        )
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ScheduleOut.model_validate(row)


@router.post("/schedules/preflight", response_model=PreflightCostResponse)
async def preflight_schedule(
    body: PreflightScheduleRequest, principal: PrincipalDep, service: ScheduleServiceDep
) -> PreflightCostResponse:
    """#603 dec-4(a): a cadence-aware cost pre-flight — "~$X/day at this cadence" for a proposed
    standing team, BEFORE GO. READ-ONLY: creates/enables NOTHING. 401 without an org; 422 on a bad
    manifest / cron. Unpriced members are surfaced (never fabricated $0)."""
    if principal.organisation_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="no organisation scope"
        )
    try:
        proj = service.preflight(
            principal,
            manifest=body.manifest,
            cron=body.cron,
            input_data=body.input_data,
            expected_in=body.expected_input_tokens,
            expected_out=body.expected_output_tokens,
        )
    except ScheduleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return PreflightCostResponse(
        cadence_fires_per_day=proj.cadence_fires_per_day,
        fleet_usd_per_day=proj.fleet_usd_per_day,
        per_member=[
            MemberCostOut(
                role=m.role,
                binding=m.binding,
                priced=m.priced,
                usd_per_fire=m.usd_per_fire,
                usd_per_day=m.usd_per_day,
            )
            for m in proj.per_member
        ],
        unpriced_members=proj.unpriced_members,
    )


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


@router.get("/schedules/{schedule_id}/team-runs", response_model=ScheduledTeamRunListResponse)
async def list_schedule_team_runs(
    schedule_id: uuid.UUID, principal: PrincipalDep, service: ScheduleServiceDep
) -> ScheduledTeamRunListResponse:
    """#601: the team-runs a standing-team schedule produced (org-scoped, newest-first) — the
    readable, gateway-only proof it fired + the PERSISTENT graph each run is bound to (the keystone:
    fire N and N+1 carry the SAME graph_id). Another tenant's schedule yields nothing."""
    rows = await service.list_team_runs(schedule_id, principal)
    out = [ScheduledTeamRunOut.model_validate(r) for r in rows]
    return ScheduledTeamRunListResponse(runs=out, total=len(out))


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
