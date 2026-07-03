"""Compiler-run route (routes layer) — parse → ONE service call → HTTP map. #635 (C-1(a)).

``POST /v1/engine/compiler-runs`` is the Describe door: prose in, a QUEUED team run out (202).
The compiler IS a normal team run (ADR-047 — no new runtime): the engine assembles the
harness-compiler team + the #596 seeded catalog server-side, binds the caller's BYOM models, and
submits through the same create path — poll the standard ``/v1/engine/team-runs/{id}`` reads,
then seed a draft via ``POST /v1/engine/team-drafts/from-run``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from oraclous_execution_engine_service.core.dependencies import (
    CompilerRunServiceDep,
    PrincipalDep,
)
from oraclous_execution_engine_service.schema.engine_schemas import (
    CreateCompilerRunRequest,
    TeamRunOut,
)
from oraclous_execution_engine_service.services.team_run_service import TeamRunError

router = APIRouter(prefix="/v1/engine", tags=["engine-compiler-runs"])


def _http(exc: TeamRunError) -> HTTPException:
    if exc.status_code == 422:
        return HTTPException(
            status_code=422,
            detail=[{"loc": ["body"], "type": exc.error_type, "msg": str(exc)}],
        )
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/compiler-runs", response_model=TeamRunOut, status_code=status.HTTP_202_ACCEPTED)
async def create_compiler_run(
    body: CreateCompilerRunRequest, principal: PrincipalDep, service: CompilerRunServiceDep
) -> TeamRunOut:
    try:
        row = await service.create(
            principal,
            objective=body.objective,
            inputs=body.inputs,
            constraints=body.constraints,
            success_criteria=body.success_criteria,
            models=body.models,
            graph_id=body.graph_id,
        )
    except TeamRunError as exc:
        raise _http(exc) from exc
    return TeamRunOut.model_validate(row)
