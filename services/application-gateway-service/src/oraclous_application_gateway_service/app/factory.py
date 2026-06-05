"""FastAPI app factory (ORAA-4 §21) — build the app, wire routers, no business logic here.

GW-1 ships the dependency-free ``/health`` probe. The reverse-proxy routes, edge JWT termination,
CORS, and upstream-health aggregation are layered on in later slices.
"""

from __future__ import annotations

from fastapi import FastAPI

from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.routes.health_routes import router as health_router


def create_app(*, lifespan=None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.APP_NAME, version=settings.VERSION, lifespan=lifespan)
    app.include_router(health_router)
    return app
