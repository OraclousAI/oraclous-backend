"""Liveness + readiness routes (ORAA-4 §21 routes layer). Unauthenticated.

``/health`` is liveness (always HTTP 200; body reflects ok/degraded so a startup store-bind failure
is visible — ADR-021). ``/readyz`` is readiness (503 when the critical store didn't bind so an
orchestrator stops routing). The critical store is Neo4j; a configured-but-failed bind is degraded,
an unset URI is intentional CRUD-only operation (not a fault). No DB access here — the route reads
the bind outcome the lifespan recorded on ``app.state``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from oraclous_telemetry import evaluate_readiness

from oraclous_knowledge_graph_service.schema.health_schemas import HealthResponse

router = APIRouter(tags=["health"])

_SERVICE = "knowledge-graph-service"


def _verdict(request: Request):
    bind_failed = getattr(request.app.state, "neo4j_bind_failed", False)
    return evaluate_readiness({"neo4j": None if bind_failed else object()})


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    return HealthResponse(status=_verdict(request).status, service=_SERVICE)


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    verdict = _verdict(request)
    return JSONResponse(
        status_code=verdict.readyz_status_code,
        content={"status": verdict.status, "service": _SERVICE},
    )
