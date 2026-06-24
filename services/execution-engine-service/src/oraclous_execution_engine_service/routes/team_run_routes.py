"""Engine team-run routes (routes layer) — parse → ONE service call → HTTP map.

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
    TeamRunCost,
    TeamRunOut,
    TeamRunStatusOut,
    TeamRunTreeOut,
)
from oraclous_execution_engine_service.services.team_run_service import TeamRunError

router = APIRouter(prefix="/v1/engine", tags=["engine-team-runs"])


def _http(exc: TeamRunError) -> HTTPException:
    """Map a ``TeamRunError`` to an ``HTTPException`` (#483 Option A). A **422** gets a
    STRUCTURED detail (``[{"loc":["body"], "type": <token>, "msg": str(exc)}]``) so the gateway's
    422 passthrough surfaces ``VALIDATION_FAILED`` with a field-level issue — instead of the
    misleading ``MALFORMED_REQUEST`` a free-string detail falls back to. The gateway drops the
    value-reflecting ``msg`` (leak-safe — Interface Contracts §3 rule 8), keeping only loc + type.
    Non-422 statuses keep a plain string detail (they already map to the right canonical code)."""
    if exc.status_code == 422:
        return HTTPException(
            status_code=422,
            detail=[{"loc": ["body"], "type": exc.error_type, "msg": str(exc)}],
        )
    return HTTPException(status_code=exc.status_code, detail=str(exc))


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
            workspace_root=body.workspace_root,
            graph_id=body.graph_id,
        )
    except TeamRunError as exc:
        raise _http(exc) from exc
    return TeamRunOut.model_validate(row)


@router.get("/team-runs/{team_run_id}", response_model=TeamRunOut)
async def get_team_run(
    team_run_id: uuid.UUID, principal: PrincipalDep, service: TeamRunServiceDep
) -> TeamRunOut:
    try:
        row = await service.get(team_run_id, principal)
    except TeamRunError as exc:
        raise _http(exc) from exc
    return TeamRunOut.model_validate(row)


@router.get("/team-runs/{team_run_id}/tree", response_model=TeamRunTreeOut)
async def get_team_run_tree(
    team_run_id: uuid.UUID, principal: PrincipalDep, service: TeamRunServiceDep
) -> TeamRunTreeOut:
    """The run-tree (#471): the root + the member harness execution ids this run dispatched. Reads
    through the SAME org-scoped ``service.get`` as the run itself — a cross-org id is a 404 (H1/H4),
    never a leak. The tree is the engine's own record (no cross-DB read into the harness)."""
    try:
        row = await service.get(team_run_id, principal)
    except TeamRunError as exc:
        raise _http(exc) from exc
    return TeamRunTreeOut(
        team_run_id=row.id,
        organisation_id=row.organisation_id,
        root_execution_id=row.root_execution_id,
        state=row.state,
        child_execution_ids=[uuid.UUID(c) for c in (row.child_execution_ids or [])],
    )


@router.get("/team-runs/{team_run_id}/status", response_model=TeamRunStatusOut)
async def get_team_run_status(
    team_run_id: uuid.UUID, principal: PrincipalDep, service: TeamRunServiceDep
) -> TeamRunStatusOut:
    """O4 light status (#472): is my team healthy / what's its progress / what did it cost — one
    glance, no full-trace machinery. Request-path org-scoped (H3): a cross-org id is a 404."""
    try:
        s = await service.status(team_run_id, principal)
    except TeamRunError as exc:
        raise _http(exc) from exc
    return TeamRunStatusOut(
        team_run_id=s.team_run_id,
        organisation_id=s.organisation_id,
        healthy=s.healthy,
        state=s.state,
        progress=s.progress,
        last_run_at=s.last_run_at,
        last_outcome=s.last_outcome,
        cost=TeamRunCost(tokens=s.cost_tokens, usd=None),
    )


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
        raise _http(exc) from exc
    return TeamRunOut.model_validate(row)
