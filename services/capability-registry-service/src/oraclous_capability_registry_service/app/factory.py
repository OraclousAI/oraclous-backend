"""FastAPI app factory — build the app, wire routers, no business logic here.

Replaces the R2 stub shell: capability descriptor CRUD + search/match are real, org-scoped, and
backed by Postgres. ``GET /health`` stays a dependency-free probe so the container is healthy even
when Postgres is unreachable (the data routes then report 503).
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from oraclous_telemetry import evaluate_readiness, install_telemetry, instrument_app

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
from oraclous_capability_registry_service.services.mcp_import_service import (
    McpCredentialError,
    McpEgressBlocked,
    McpImportError,
    McpServerRefusedAuth,
    McpServerTimeout,
)
from oraclous_capability_registry_service.services.tool_execution_service import (
    ExecutionNotReadyError,
)


def create_app(*, lifespan=None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.APP_NAME, version=settings.VERSION, lifespan=lifespan)
    install_telemetry(app)  # WP-6: JSON structured logging + correlation-id middleware
    instrument_app(app, with_neo4j=False)  # #366: OTel tracing (no-op unless OTEL endpoint set)
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

    # ── #715: an MCP import failure says WHICH failure it was ────────────────────────────────────
    #
    # The status chosen here decides the ORA-56 code the console finally sees: the gateway proxies
    # /api/v1/tools and maps the upstream status through status_to_code, with two shape-driven
    # exits — a 422 carrying FastAPI-shaped `detail` becomes VALIDATION_FAILED, and a 409 carrying a
    # TOP-LEVEL `needs_credential` becomes CREDENTIALS_REQUIRED. 401/403 are deliberately unused for
    # the MCP server's refusal: they map to UNAUTHENTICATED/UNAUTHORIZED, which the console reads as
    # the caller's own session being bad. Starlette resolves handlers along the exception's MRO, so
    # each subclass below wins over the McpImportError catch-all. Mapping under Contract #720.

    @app.exception_handler(McpEgressBlocked)
    async def _on_mcp_egress_blocked(_: Request, __: McpEgressBlocked) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "the MCP server URL is not an allowed external target"},
        )

    @app.exception_handler(McpCredentialError)
    async def _on_mcp_credential(_: Request, exc: McpCredentialError) -> JSONResponse:
        # OUR failure, not the MCP server's: the caller's credential_id is what is wrong. The
        # FastAPI field-error LIST is the shape the gateway turns into VALIDATION_FAILED naming the
        # field; a string `detail` would degrade to a generic MALFORMED_REQUEST and lose the reason.
        # `reason` is a constant on the class — never a value derived from the credential itself.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": [{"loc": ["body", "credential_id"], "type": exc.reason}]},
        )

    @app.exception_handler(McpServerRefusedAuth)
    async def _on_mcp_auth_refusal(_: Request, __: McpServerRefusedAuth) -> JSONResponse:
        # The token must sit at the TOP level, mirroring ExecutionNotReadyError above — nested under
        # `detail` the gateway's extract_needs_credential cannot see it. Both values are constants
        # authored here, matching the requirement the importer declares on an auth'd import.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "the MCP server refused the request for authentication",
                "needs_credential": {"requirement_id": "api_key", "provider": "mcp"},
            },
        )

    @app.exception_handler(McpServerTimeout)
    async def _on_mcp_timeout(_: Request, __: McpServerTimeout) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"detail": "the MCP server did not answer in time"},
        )

    @app.exception_handler(McpImportError)
    async def _on_mcp_import(_: Request, __: McpImportError) -> JSONResponse:
        # Everything left: unreachable, a rejected handshake, a malformed body, any other non-2xx.
        # The MCP server really is unusable, so SERVICE_UNAVAILABLE is honest here.
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "could not import from the MCP server"},
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
