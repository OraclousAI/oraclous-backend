"""Liveness + readiness routes (routes layer). Unauthenticated.

``/health`` is liveness (always 200; body reflects ok/degraded — ADR-021). ``/readyz`` is readiness
(503 when the critical store didn't bind). The critical store is Postgres (the job repository). No
DB access here — the route reads the bind outcome the lifespan left on ``app.state``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from oraclous_telemetry import evaluate_readiness

from oraclous_execution_engine_service.schema.engine_schemas import HealthResponse

router = APIRouter(tags=["health"])

_SERVICE = "execution-engine-service"


def _verdict(request: Request):
    return evaluate_readiness({"postgres": getattr(request.app.state, "job_repository", None)})


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
