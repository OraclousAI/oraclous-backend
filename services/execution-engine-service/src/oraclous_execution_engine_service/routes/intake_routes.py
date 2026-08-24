"""Intake routes (routes layer) — parse → ONE service call → HTTP map. #866.

The validation desk's read-back: the only user-facing door onto reading a founder's idea before
the run starts. Three outcomes cross it — the read-back itself, a run id when the model is slower
than the poll budget, and the two refusals.

The refusals are shaped deliberately. The gateway drains an upstream error body rather than
relaying it, so an allow-listed ``error_code`` is the only thing that survives the edge and reaches
the browser; a refusal without one falls back to the ordinary structured-422 path.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from oraclous_execution_engine_service.core.dependencies import (
    IntakeReadbackServiceDep,
    PrincipalDep,
)
from oraclous_execution_engine_service.schema.engine_schemas import (
    IntakeReadbackOut,
    IntakeReadbackPendingOut,
    IntakeReadbackRequest,
    ReadbackQuestion,
    ReadbackSpan,
)
from oraclous_execution_engine_service.services.intake_readback_service import (
    IntakeReadbackError,
    PendingReadback,
)

router = APIRouter(prefix="/v1/engine", tags=["engine-intake"])


def _http(exc: IntakeReadbackError) -> HTTPException:
    if exc.error_code is not None:
        # The one shape the gateway's allow-list reads. Nothing else from this body crosses the
        # edge, so the code has to carry the whole meaning of the refusal on its own.
        return HTTPException(status_code=exc.status_code, detail={"error_code": exc.error_code})
    # #483 Option A: a STRUCTURED 422 detail (leak-safe machine token in `type`) so the gateway
    # maps it to VALIDATION_FAILED + a field-level issue; other statuses keep a plain detail.
    if exc.status_code == 422:
        return HTTPException(
            status_code=422,
            detail=[{"loc": ["body"], "type": exc.error_type, "msg": str(exc)}],
        )
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/intake/readback", response_model=IntakeReadbackOut)
async def intake_readback(
    body: IntakeReadbackRequest,
    principal: PrincipalDep,
    svc: IntakeReadbackServiceDep,
) -> IntakeReadbackOut | JSONResponse:
    try:
        outcome = await svc.readback(
            principal,
            idea=body.idea,
            models=body.models,
            readback_run_id=body.readback_run_id,
        )
    except IntakeReadbackError as exc:
        raise _http(exc) from exc
    if isinstance(outcome, PendingReadback):
        # 202: the reader is still driving. Slow is not failed — the caller re-calls with this id.
        return JSONResponse(
            status_code=202,
            content=IntakeReadbackPendingOut(readback_run_id=outcome.readback_run_id).model_dump(
                mode="json"
            ),
        )
    return IntakeReadbackOut(
        restatement=[ReadbackSpan(text=s.text, source=s.source) for s in outcome.restatement],
        questions=[
            ReadbackQuestion(id=q.id, text=q.text, kind=q.kind, options=q.options)
            for q in outcome.questions
        ],
    )
