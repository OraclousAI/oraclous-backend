"""Engine team-run routes (ORAA-4 §21 routes layer) — parse → ONE service call → HTTP map.

The reachable entry point for the team orchestrator (it had none — ORAA E3): an OHM v1.1 Team
Harness can now be run over HTTP.

``POST /v1/engine/team-runs`` runs a Team Harness (the team manifest + per-role sub-harnesses),
driving its member DAG through the real harness and persisting the run; ``GET
/v1/engine/team-runs/{id}`` reads it; ``POST .../advance`` records a human-gate decision on a PAUSED
run and re-drives past it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_execution_engine_service.core.dependencies import PrincipalDep, TeamRunServiceDep
from oraclous_execution_engine_service.schema.engine_schemas import (
    AdvanceTeamRunRequest,
    CreateTeamRunRequest,
    TeamRunOut,
)
from oraclous_execution_engine_service.services.team_run_service import TeamRunError

router = APIRouter(prefix="/v1/engine", tags=["engine-team-runs"])


@router.post("/team-runs", response_model=TeamRunOut, status_code=status.HTTP_202_ACCEPTED)
async def create_team_run(
    body: CreateTeamRunRequest, principal: PrincipalDep, service: TeamRunServiceDep
) -> TeamRunOut:
    # 202: the run is validated + persisted QUEUED and handed to the worker, which drives the team
    # asynchronously (a 30-agent team would otherwise block the request). Poll GET for state.
    try:
        row = await service.create(
            principal,
            manifest=body.manifest,
            sub_harnesses=body.sub_harnesses,
            gate_decisions=body.gate_decisions,
        )
    except TeamRunError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return TeamRunOut.model_validate(row)


@router.get("/team-runs/{team_run_id}", response_model=TeamRunOut)
async def get_team_run(
    team_run_id: uuid.UUID, principal: PrincipalDep, service: TeamRunServiceDep
) -> TeamRunOut:
    try:
        row = await service.get(team_run_id, principal)
    except TeamRunError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return TeamRunOut.model_validate(row)


@router.post(
    "/team-runs/{team_run_id}/advance",
    response_model=TeamRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def advance_team_run(
    team_run_id: uuid.UUID,
    body: AdvanceTeamRunRequest,
    principal: PrincipalDep,
    service: TeamRunServiceDep,
) -> TeamRunOut:
    try:
        row = await service.advance(team_run_id, principal, body.gate_decisions)
    except TeamRunError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return TeamRunOut.model_validate(row)
