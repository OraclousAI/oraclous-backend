"""Evaluation route (routes layer) (#331).

POST /v1/graph/{graph_id}/evaluate — RAGAS-style retrieval-quality scoring (faithfulness,
answer_relevance, context_precision, context_recall) over the EXISTING KRS retrieval path,
judged natively by the configured LLM (no ragas lib). Explicit endpoint only — it is never
hooked into a chat path. Auth/org-scoping is the standard KRS dependency chain (ADR-018);
another org's graph (or a nonexistent one) is a 404. Thin: parse → one service call → map
typed service errors to statuses.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_retriever_service.core.dependencies import EvaluationServiceDep, UserIdDep
from oraclous_knowledge_retriever_service.schema.evaluation_schemas import (
    EvaluationRequest,
    EvaluationResponse,
)
from oraclous_knowledge_retriever_service.services.evaluation_service import (
    EvaluationCapacityExceeded,
    GraphNotFound,
    NoValidMetrics,
)

router = APIRouter(prefix="/v1/graph", tags=["evaluation"])


@router.post(
    "/{graph_id}/evaluate",
    response_model=EvaluationResponse,
    summary="Evaluate retrieval quality with native RAGAS-style LLM-judge metrics",
    responses={
        404: {"description": "Graph not found in the caller's organisation"},
        422: {"description": "Invalid request, no computable metrics, or judge not configured"},
        429: {"description": "Too many evaluations in flight (process-level judge-spend cap)"},
    },
)
async def evaluate_graph(
    graph_id: uuid.UUID,
    body: EvaluationRequest,
    service: EvaluationServiceDep,
    _user_id: UserIdDep,
) -> EvaluationResponse:
    # Typed 422s use the Pydantic LIST detail shape — the only shape the gateway's leak-safe
    # #225 extractor relays (loc + type survive the edge as VALIDATION_FAILED details, #333).
    try:
        result = await service.evaluate(
            graph_id=str(graph_id),
            question=body.question,
            answer=body.answer,
            ground_truth=body.ground_truth,
            metrics=body.metrics,
        )
    except GraphNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="graph not found"
        ) from None
    except NoValidMetrics as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"loc": ["body", "metrics"], "type": "no_valid_metrics", "msg": str(exc)}],
        ) from None
    except EvaluationCapacityExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=[{"loc": ["eval"], "type": "eval_capacity_exceeded", "msg": str(exc)}],
        ) from None
    return EvaluationResponse(graph_id=str(graph_id), question=body.question, **result)
