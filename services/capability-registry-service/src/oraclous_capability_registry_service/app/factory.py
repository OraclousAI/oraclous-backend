"""FastAPI app factory (ORAA-4 §21) — build the app, wire routers, no business logic here.

Replaces the R2 stub shell: capability descriptor CRUD + search/match are real, org-scoped, and
backed by Postgres. ``GET /health`` stays a dependency-free probe so the container is healthy even
when Postgres is unreachable (the data routes then report 503).
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.errors import (
    CapabilityNotFoundError,
    InvalidDescriptorError,
)
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityConflictError,
)
from oraclous_capability_registry_service.routes.capability_routes import (
    router as capability_router,
)
from oraclous_capability_registry_service.routes.execution_routes import router as execution_router
from oraclous_capability_registry_service.routes.instance_routes import router as instance_router
from oraclous_capability_registry_service.routes.tool_routes import router as tool_router
from oraclous_capability_registry_service.services.instance_manager import InstanceNotFoundError
from oraclous_capability_registry_service.services.tool_execution_service import (
    ExecutionNotReadyError,
)


def create_app(*, lifespan=None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.APP_NAME, version=settings.VERSION, lifespan=lifespan)
    app.include_router(capability_router)
    app.include_router(tool_router)
    app.include_router(instance_router)
    app.include_router(execution_router)

    @app.exception_handler(CapabilityNotFoundError)
    async def _on_not_found(_: Request, exc: CapabilityNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.exception_handler(CapabilityConflictError)
    async def _on_conflict(_: Request, exc: CapabilityConflictError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.exception_handler(InstanceNotFoundError)
    async def _on_instance_not_found(_: Request, exc: InstanceNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.exception_handler(ExecutionNotReadyError)
    async def _on_not_ready(_: Request, exc: ExecutionNotReadyError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "error_code": exc.error_code, **exc.detail},
        )

    @app.exception_handler(InvalidDescriptorError)
    async def _on_invalid(_: Request, exc: InvalidDescriptorError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
        )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy", "service": "capability-registry", "version": settings.VERSION}

    @app.get("/api/v1/health")
    async def api_v1_health() -> dict:
        return {"status": "healthy", "service": "capability-registry", "version": settings.VERSION}

    return app
