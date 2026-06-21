"""Gateway health routes (routes layer).

``GET /health`` is a dependency-free liveness probe — it answers even when every upstream is down,
so the container is healthy independently of the substrate. ``GET /health/upstreams`` aggregates
each upstream's ``/health`` (per-service status + an overall rollup); it always returns 200 and the
body reflects the substrate state.
"""

from __future__ import annotations

from fastapi import APIRouter

from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.core.dependencies import HealthServiceDep
from oraclous_application_gateway_service.schema.health import UpstreamsHealthResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {"status": "ok", "service": "application-gateway", "version": settings.VERSION}


@router.get("/health/upstreams", response_model=UpstreamsHealthResponse)
async def upstreams_health(svc: HealthServiceDep) -> UpstreamsHealthResponse:
    return await svc.check_all()
