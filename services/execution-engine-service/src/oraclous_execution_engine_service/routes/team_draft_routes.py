"""Team-draft routes (routes layer) — parse → ONE service call → HTTP map. #635 (C-1).

The draft store's HTTP surface: CRUD, every write embedding the shared validator's verdict. GO
stays ``POST /v1/engine/team-runs`` with the draft's documents — a draft never executes. NOTE:
the collection path is registered BEFORE ``/team-drafts/{team_draft_id}`` so it is never
captured as an id.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from oraclous_execution_engine_service.core.dependencies import (
    PrincipalDep,
    TeamDraftServiceDep,
)
from oraclous_execution_engine_service.schema.engine_schemas import (
    CreateTeamDraftRequest,
    TeamDraftEnvelope,
    TeamDraftListItem,
    TeamDraftListOut,
    TeamDraftOut,
)
from oraclous_execution_engine_service.services.team_draft_service import DraftVerdict
from oraclous_execution_engine_service.services.team_run_service import TeamRunError

router = APIRouter(prefix="/v1/engine", tags=["engine-team-drafts"])


def _http(exc: TeamRunError) -> HTTPException:
    # #483 Option A: a STRUCTURED 422 detail (leak-safe machine token in `type`) so the gateway
    # maps it to VALIDATION_FAILED + a field-level issue; other statuses keep a plain detail.
    if exc.status_code == 422:
        return HTTPException(
            status_code=422,
            detail=[{"loc": ["body"], "type": exc.error_type, "msg": str(exc)}],
        )
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _envelope(row: object, verdict: DraftVerdict) -> TeamDraftEnvelope:
    return TeamDraftEnvelope(
        draft=TeamDraftOut.model_validate(row),
        would_block=verdict.would_block,
        blocking=verdict.blocking,
        report=verdict.report,
    )


@router.post("/team-drafts", response_model=TeamDraftEnvelope, status_code=status.HTTP_201_CREATED)
async def create_team_draft(
    body: CreateTeamDraftRequest, principal: PrincipalDep, service: TeamDraftServiceDep
) -> TeamDraftEnvelope:
    try:
        row, verdict = await service.create(
            principal, name=body.name, manifest=body.manifest, sub_harnesses=body.sub_harnesses
        )
    except TeamRunError as exc:
        raise _http(exc) from exc
    return _envelope(row, verdict)


@router.get("/team-drafts", response_model=TeamDraftListOut)
async def list_team_drafts(
    principal: PrincipalDep,
    service: TeamDraftServiceDep,
    limit: Annotated[int, Query()] = 50,
    offset: Annotated[int, Query()] = 0,
) -> TeamDraftListOut:
    """The org's drafts, newest-first, paginated (``limit`` default 50 / max 200, ``offset``
    default 0 — both clamped server-side; born bounded, WP-10). REGISTERED BEFORE
    ``/team-drafts/{team_draft_id}`` so the bare collection path is never captured as an id."""
    try:
        rows, total = await service.list_for_org(principal, limit=limit, offset=offset)
    except TeamRunError as exc:  # a principal with no org → the contracted 403, not a 500
        raise _http(exc) from exc
    return TeamDraftListOut(
        team_drafts=[TeamDraftListItem.model_validate(r) for r in rows], total=total
    )


@router.get("/team-drafts/{team_draft_id}", response_model=TeamDraftEnvelope)
async def get_team_draft(
    team_draft_id: uuid.UUID, principal: PrincipalDep, service: TeamDraftServiceDep
) -> TeamDraftEnvelope:
    try:
        row, verdict = await service.get(team_draft_id, principal)
    except TeamRunError as exc:
        raise _http(exc) from exc
    return _envelope(row, verdict)


@router.put("/team-drafts/{team_draft_id}", response_model=TeamDraftEnvelope)
async def replace_team_draft(
    team_draft_id: uuid.UUID,
    body: CreateTeamDraftRequest,
    principal: PrincipalDep,
    service: TeamDraftServiceDep,
) -> TeamDraftEnvelope:
    """Full replace — the whole document swaps and ``version`` bumps (the client's
    concurrent-edit signal)."""
    try:
        row, verdict = await service.replace(
            team_draft_id,
            principal,
            name=body.name,
            manifest=body.manifest,
            sub_harnesses=body.sub_harnesses,
        )
    except TeamRunError as exc:
        raise _http(exc) from exc
    return _envelope(row, verdict)


@router.delete("/team-drafts/{team_draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team_draft(
    team_draft_id: uuid.UUID, principal: PrincipalDep, service: TeamDraftServiceDep
) -> Response:
    try:
        await service.delete(team_draft_id, principal)
    except TeamRunError as exc:
        raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
