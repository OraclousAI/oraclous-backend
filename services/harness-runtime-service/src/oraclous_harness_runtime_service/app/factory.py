"""Application assembly (ORAA-4 §21 app layer) — build FastAPI, include routers, NO handlers."""

from __future__ import annotations

from fastapi import FastAPI
from oraclous_telemetry import install_telemetry, instrument_app

from oraclous_harness_runtime_service.core.lifespan import lifespan
from oraclous_harness_runtime_service.routes import harness_routes, health_routes


def create_app() -> FastAPI:
    app = FastAPI(title="harness-runtime-service", version="0.1.0", lifespan=lifespan)
    install_telemetry(app)  # WP-6: JSON structured logging + correlation-id middleware
    instrument_app(app, with_neo4j=False)  # #366: OTel tracing (no-op unless OTEL endpoint set)
    app.include_router(health_routes.router)
    app.include_router(harness_routes.router)
    return app
