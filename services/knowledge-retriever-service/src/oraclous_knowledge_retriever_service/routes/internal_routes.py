"""Internal-plane routes (ORAA-4 §21 routes layer) — the agent/capability-addressable surface.

`POST /internal/v1/evaluate` is the backing of the `core/evaluate` capability (ADR-037 / #469): a
caller (the harness orchestration agent, the named battery #470, the E8 loop) grades an output
against a `success_criteria` and gets the typed `Verdict`. Reached over the internal trust path
(gateway mode: X-Internal-Key + forwarded X-Principal-*; dev/jwt: bearer), so the org is bound from
the verified principal. The graded output's org is **server-stamped from that principal** (ADR-037
H2) — never the request body — so a caller can never forge a foreign-org verdict.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from oraclous_eval import EvaluationCapacityExceeded, Verdict
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_retriever_service.core.dependencies import FlowEvalServiceDep
from oraclous_knowledge_retriever_service.schema.flow_eval_schemas import FlowEvaluateRequest
from oraclous_knowledge_retriever_service.services.flow_evaluation_service import (
    BatteryNotSupported,
)

router = APIRouter(prefix="/internal/v1", tags=["internal"])


@router.post("/evaluate", response_model=Verdict, response_model_by_alias=True)
async def evaluate_flow(body: FlowEvaluateRequest, service: FlowEvalServiceDep) -> Verdict:
    """Grade `target_output` against `success_criteria`, returning the structured `Verdict`. The
    org is the bound principal's (server-stamped, H2). A `battery:<name>` criterion is the named-
    battery path (#470) — 422 here; an exhausted evaluation slot pool → 429 (fail-closed caps)."""
    organisation_id = enforced_organisation_id()  # the verified principal's org (ADR-037 H2)
    try:
        return await service.evaluate(
            target_kind=body.target_kind,
            target_ref=body.target_ref,
            target_output=body.target_output,
            success_criteria=body.success_criteria,
            organisation_id=organisation_id,
            pass_threshold=body.pass_threshold,
        )
    except BatteryNotSupported:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="named gate batteries (battery:<name>) land with #470",
        ) from None
    except EvaluationCapacityExceeded:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="evaluation capacity exceeded"
        ) from None
