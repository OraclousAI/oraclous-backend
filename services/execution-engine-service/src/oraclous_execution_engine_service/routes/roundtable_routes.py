"""Engine round-table routes (routes layer) — parse → ONE service call → HTTP map.

``POST /v1/engine/roundtables`` starts a round-table (202; the driver runs agent turns async);
``GET /v1/engine/roundtables/{id}`` reads it; ``POST /v1/engine/roundtables/{id}/respond`` submits a
human turn, which appends to the transcript and resumes the driver.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_execution_engine_service.core.dependencies import PrincipalDep, RoundtableServiceDep
from oraclous_execution_engine_service.schema.engine_schemas import (
    CreateRoundtableRequest,
    RespondRoundtableRequest,
    RoundtableOut,
)
from oraclous_execution_engine_service.services.roundtable_service import RoundtableError

router = APIRouter(prefix="/v1/engine", tags=["engine-roundtables"])


@router.post("/roundtables", response_model=RoundtableOut, status_code=status.HTTP_202_ACCEPTED)
async def create_roundtable(
    body: CreateRoundtableRequest, principal: PrincipalDep, service: RoundtableServiceDep
) -> RoundtableOut:
    try:
        row = await service.create(
            principal,
            topic=body.topic,
            actors=[a.model_dump() for a in body.actors],
            max_rounds=body.max_rounds,
        )
    except RoundtableError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return RoundtableOut.model_validate(row)


@router.get("/roundtables/{roundtable_id}", response_model=RoundtableOut)
async def get_roundtable(
    roundtable_id: uuid.UUID, principal: PrincipalDep, service: RoundtableServiceDep
) -> RoundtableOut:
    row = await service.get(roundtable_id, principal)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="round-table not found")
    return RoundtableOut.model_validate(row)


@router.post("/roundtables/{roundtable_id}/respond", response_model=RoundtableOut)
async def respond_roundtable(
    roundtable_id: uuid.UUID,
    body: RespondRoundtableRequest,
    principal: PrincipalDep,
    service: RoundtableServiceDep,
) -> RoundtableOut:
    try:
        row = await service.respond(roundtable_id, principal, body.output)
    except RoundtableError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return RoundtableOut.model_validate(row)
