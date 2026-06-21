"""FastAPI app factory — build the app, wire routers, no business logic here."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from oraclous_telemetry import evaluate_readiness, install_telemetry, instrument_app

from oraclous_credential_broker_service.routes.credential_routes import router as credential_router
from oraclous_credential_broker_service.routes.internal_routes import router as internal_router
from oraclous_credential_broker_service.services.credential_service import CredentialNotFoundError


def create_app(*, lifespan=None) -> FastAPI:
    app = FastAPI(title="oraclous-credential-broker-service", version="0.0.1", lifespan=lifespan)
    install_telemetry(app)  # WP-6: JSON structured logging + correlation-id middleware
    instrument_app(app, with_neo4j=False)  # #366: OTel tracing (no-op unless OTEL endpoint set)
    app.include_router(credential_router)
    app.include_router(internal_router)

    @app.exception_handler(CredentialNotFoundError)
    async def _on_not_found(_: Request, exc: CredentialNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.get("/health")
    async def health(request: Request) -> dict:
        # Liveness — always 200; body reflects ok/degraded so a startup store-bind failure is
        # visible (ADR-021). The critical store is Postgres (the credential repository).
        verdict = evaluate_readiness(
            {"postgres": getattr(request.app.state, "credential_repository", None)}
        )
        status_label = "healthy" if not verdict.is_degraded else verdict.status
        return {"status": status_label, "service": "credential-broker"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        # Readiness — 503 when the critical store didn't bind so an orchestrator stops routing.
        verdict = evaluate_readiness(
            {"postgres": getattr(request.app.state, "credential_repository", None)}
        )
        status_label = "healthy" if not verdict.is_degraded else verdict.status
        return JSONResponse(
            status_code=verdict.readyz_status_code,
            content={"status": status_label, "service": "credential-broker"},
        )

    return app
