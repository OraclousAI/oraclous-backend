"""FastAPI app factory (ORAA-4 §21) — build the app, wire routers, no business logic here.

Replaces the R2 stub shell: capability descriptor CRUD + search/match are real, org-scoped, and
backed by Postgres. ``GET /health`` stays a dependency-free probe so the container is healthy even
when Postgres is unreachable (the data routes then report 503).
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from oraclous_telemetry import evaluate_readiness

from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.errors import (
    CapabilityNotFoundError,
    InvalidDescriptorError,
)
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityConflictError,
)
from oraclous_capability_registry_service.routes.binding_routes import router as binding_router
from oraclous_capability_registry_service.routes.capability_routes import (
    router as capability_router,
)
from oraclous_capability_registry_service.routes.execution_routes import router as execution_router
from oraclous_capability_registry_service.routes.instance_routes import router as instance_router
from oraclous_capability_registry_service.routes.tool_routes import router as tool_router
from oraclous_capability_registry_service.services.graph_membership_client import (
    GraphMembershipError,
)
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
    app.include_router(binding_router)

    @app.exception_handler(CapabilityNotFoundError)
    async def _on_not_found(_: Request, exc: CapabilityNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.exception_handler(GraphMembershipError)
    async def _on_graph_membership(_: Request, __: GraphMembershipError) -> JSONResponse:
        # The KGS membership check (the graph-side visibility verify) could not be reached — a 503
        # (transient upstream). The upstream body is never echoed (no-leak); the detail is curated.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "workspace verification is temporarily unavailable"},
        )

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

    def _health_body(request: Request) -> dict:
        # Liveness body — reflects ok/degraded so a startup store-bind failure is visible
        # (ADR-021). The critical store is Postgres (the capability repository).
        verdict = evaluate_readiness(
            {"postgres": getattr(request.app.state, "capability_repository", None)}
        )
        status_label = "healthy" if not verdict.is_degraded else verdict.status
        return {
            "status": status_label,
            "service": "capability-registry",
            "version": settings.VERSION,
        }

    @app.get("/health")
    async def health(request: Request) -> dict:
        return _health_body(request)

    @app.get("/api/v1/health")
    async def api_v1_health(request: Request) -> dict:
        return _health_body(request)

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        # Readiness — 503 when the critical store didn't bind so an orchestrator stops routing.
        verdict = evaluate_readiness(
            {"postgres": getattr(request.app.state, "capability_repository", None)}
        )
        body = _health_body(request)
        return JSONResponse(status_code=verdict.readyz_status_code, content=body)

    return app
