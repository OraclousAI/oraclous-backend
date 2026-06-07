"""Application assembly (ORAA-4 §21 app layer) — build FastAPI, include routers, NO handlers."""

from __future__ import annotations

from fastapi import FastAPI

from oraclous_execution_engine_service.core.lifespan import lifespan
from oraclous_execution_engine_service.routes import (
    health_routes,
    job_routes,
    schedule_routes,
    task_routes,
)


def create_app() -> FastAPI:
    app = FastAPI(title="execution-engine-service", version="0.1.0", lifespan=lifespan)
    app.include_router(health_routes.router)
    app.include_router(job_routes.router)
    app.include_router(task_routes.router)
    app.include_router(schedule_routes.router)
    return app
