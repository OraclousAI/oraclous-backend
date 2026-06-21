"""Internal-plane routes (routes layer) — the agent/capability-addressable surface.

`POST /internal/v1/evaluate` is the backing of the `core/evaluate` capability (ADR-037 / #469): a
caller (the harness orchestration agent, the named battery #470, the team-run gate #477) grades an
output against a `success_criteria` and gets the typed `Verdict`. Reached over the internal trust
path (gateway mode: X-Internal-Key + forwarded X-Principal-*; dev/jwt: bearer), so the org is bound
from the verified principal. The graded output's org is **server-stamped from that principal**
(ADR-037 H2) — never the request body — so a caller can never forge a foreign-org verdict.

The judge is the operator singleton (`KRS_OPENAI_API_KEY`) by default; when the request carries a
`judge_credential_id` (a manifest `role="evaluator"` model's credential), KRS resolves THAT per-org
key from the credential-broker and grades with the caller's own key — the BYOM-judge path: the
user's key arrives via the gateway credentials API, never a server env (ADR-037 / BYOM-judge).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from oraclous_eval import EvaluationCapacityExceeded, Verdict
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_retriever_service.core.config import get_settings
from oraclous_knowledge_retriever_service.core.dependencies import (
    EvalJudge,
    OrganisationContext,
    bind_org_context,
    build_flow_eval_service,
    get_eval_judge,
    get_eval_judge_optional,
)
from oraclous_knowledge_retriever_service.schema.flow_eval_schemas import FlowEvaluateRequest
from oraclous_knowledge_retriever_service.services.broker_client import BrokerError
from oraclous_knowledge_retriever_service.services.eval_judge import resolve_byom_judge
from oraclous_knowledge_retriever_service.services.flow_evaluation_service import (
    BatteryNotSupported,
    FlowEvaluationService,
)

router = APIRouter(prefix="/internal/v1", tags=["internal"])


@router.post("/evaluate", response_model=Verdict, response_model_by_alias=True)
async def evaluate_flow(
    body: FlowEvaluateRequest,
    request: Request,
    _org: Annotated[OrganisationContext, Depends(bind_org_context)],
    judge_singleton: Annotated[EvalJudge | None, Depends(get_eval_judge_optional)],
) -> Verdict:
    """Grade `target_output` against `success_criteria`, returning the structured `Verdict`. The org
    is the bound principal's (server-stamped, H2). With a `judge_credential_id`, the per-org BYOM
    key is resolved from the broker and used for THIS request; otherwise the operator singleton (or
    its typed 422). A `battery:<name>` criterion → 422 (#470); an exhausted slot pool → 429."""
    organisation_id = enforced_organisation_id()  # the verified principal's org (ADR-037 H2)
    settings = get_settings()

    if (
        body.judge_credential_id
    ):  # BYOM judge — resolve the caller's own key per-org from the broker
        try:
            judge = await resolve_byom_judge(
                settings,
                credential_id=body.judge_credential_id,
                judge_model=body.judge_model,
                organisation_id=uuid.UUID(organisation_id),  # broker resolve is UUID-typed
            )
        except BrokerError as exc:
            raise HTTPException(  # fail-closed: an unresolvable BYOM credential is a typed 422
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=[
                    {
                        "loc": ["judge_credential_id"],
                        "type": "byom_judge_credential_unavailable",
                        "msg": str(exc),
                    }
                ],
            ) from exc
        try:
            service = build_flow_eval_service(judge, request, settings)
            return await _evaluate(service, body, organisation_id)
        finally:
            await judge.aclose()  # per-request judge — close its HTTP client; never the singleton

    # operator-key fallback: the lifespan singleton, or the typed 422 when none is configured
    judge_singleton = judge_singleton or get_eval_judge(request)  # raises the typed 422 when None
    service = build_flow_eval_service(judge_singleton, request, settings)
    return await _evaluate(service, body, organisation_id)


async def _evaluate(
    service: FlowEvaluationService, body: FlowEvaluateRequest, organisation_id: str
) -> Verdict:
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
