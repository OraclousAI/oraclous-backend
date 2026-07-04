"""Team-draft routes (routes layer) — parse → ONE service call → HTTP map. #635 (C-1).

The compile → review → refine loop's HTTP surface: the draft store (CRUD, every write embedding
the shared validator's verdict), draft-from-run (peel a SUCCEEDED compiler run's reviewer JSON
into a draft), and refine / refine-nl (ONE typed op, applied server-side with preserve-the-rest
guaranteed). GO stays ``POST /v1/engine/team-runs`` with the draft's documents — a draft never
executes. NOTE: the two static collection paths (``/team-drafts`` and ``/team-drafts/from-run``)
are registered BEFORE ``/team-drafts/{team_draft_id}`` so neither is captured as an id.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from oraclous_execution_engine_service.core.dependencies import (
    PrincipalDep,
    TeamDraftServiceDep,
)
from oraclous_execution_engine_service.schema.engine_schemas import (
    CreateTeamDraftRequest,
    RefineTeamDraftNlPendingOut,
    RefineTeamDraftNlRequest,
    RefineTeamDraftOut,
    RefineTeamDraftRequest,
    TeamDraftEnvelope,
    TeamDraftFromRunRequest,
    TeamDraftListItem,
    TeamDraftListOut,
    TeamDraftOut,
)
from oraclous_execution_engine_service.services.team_draft_service import (
    DraftVerdict,
    PendingOpDraft,
    RefineOutcome,
)
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


def _refine_out(outcome: RefineOutcome) -> RefineTeamDraftOut:
    return RefineTeamDraftOut(
        op=outcome.op,
        applied=outcome.applied,
        would_block=outcome.verdict.would_block,
        blocking=outcome.verdict.blocking,
        report=outcome.verdict.report,
        draft=TeamDraftOut.model_validate(outcome.row),
        op_drafter_run_id=outcome.op_drafter_run_id,
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


@router.post(
    "/team-drafts/from-run",
    response_model=TeamDraftEnvelope,
    status_code=status.HTTP_201_CREATED,
)
async def create_team_draft_from_run(
    body: TeamDraftFromRunRequest,
    principal: PrincipalDep,
    service: TeamDraftServiceDep,
    response: Response,
) -> TeamDraftEnvelope:
    """Peel a SUCCEEDED (compiler) run's reviewer JSON into a stored draft (#635 concern 3).
    Ineligible/unparseable runs are a curated 422 — nothing persisted.

    #638 idempotency: ONE draft per ``(org, team_run_id)`` — **201 Created** on the first call,
    **200 OK** returning the SAME (existing) draft on a repeat (a reload / second tab on
    ``?compile=<runId>``, or a concurrent from-run that lost the race). A client can key off the
    status; the body is the identical envelope either way, so re-POSTing is always safe."""
    try:
        row, verdict, created = await service.create_from_run(
            principal, team_run_id=body.team_run_id, name=body.name
        )
    except TeamRunError as exc:
        raise _http(exc) from exc
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return _envelope(row, verdict)


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


@router.post("/team-drafts/{team_draft_id}/refine", response_model=RefineTeamDraftOut)
async def refine_team_draft(
    team_draft_id: uuid.UUID,
    body: RefineTeamDraftRequest,
    principal: PrincipalDep,
    service: TeamDraftServiceDep,
) -> RefineTeamDraftOut:
    """Apply ONE typed op (``apply_refine``, preserve-the-rest guaranteed) + re-validate +
    version-bump. A blocked op → ``applied: false``, draft untouched. ``dry_run`` previews."""
    try:
        outcome = await service.refine(
            team_draft_id, principal, edit_op=body.edit_op, dry_run=body.dry_run
        )
    except TeamRunError as exc:
        raise _http(exc) from exc
    return _refine_out(outcome)


@router.post(
    "/team-drafts/{team_draft_id}/refine-nl",
    response_model=RefineTeamDraftOut,
    responses={
        202: {
            "model": RefineTeamDraftNlPendingOut,
            "description": "The op-drafter run is still driving — re-call with its"
            " op_drafter_run_id to collect.",
        }
    },
)
async def refine_team_draft_nl(
    team_draft_id: uuid.UUID,
    body: RefineTeamDraftNlRequest,
    principal: PrincipalDep,
    service: TeamDraftServiceDep,
) -> RefineTeamDraftOut | JSONResponse:
    """NL refine (#595): the op-drafter (a one-member team run, the caller's BYOM models bound)
    turns the instruction into ONE typed op, then the same apply path as ``refine``. Polls the
    drafter only up to a budget under the gateway's upstream read timeout — a slower draft
    returns 202 + ``op_drafter_run_id``; re-call with that id to collect. ``dry_run`` returns
    the op + verdict without applying."""
    try:
        outcome = await service.refine_nl(
            team_draft_id,
            principal,
            instruction=body.instruction,
            models=body.models,
            op_drafter_run_id=body.op_drafter_run_id,
            dry_run=body.dry_run,
        )
    except TeamRunError as exc:
        raise _http(exc) from exc
    if isinstance(outcome, PendingOpDraft):
        pending = RefineTeamDraftNlPendingOut(op_drafter_run_id=outcome.op_drafter_run_id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content=pending.model_dump(mode="json")
        )
    return _refine_out(outcome)
