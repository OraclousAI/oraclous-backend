"""Gateway health routes (ORAA-4 §21 routes layer).

``GET /health`` is a dependency-free liveness probe — it must answer even when every upstream is
down, so the container is reported healthy independently of the substrate. Upstream-aggregating
health (``GET /health/upstreams``) is added in a later slice.
"""

from __future__ import annotations

from fastapi import APIRouter

from oraclous_application_gateway_service.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {"status": "ok", "service": "application-gateway", "version": settings.VERSION}
