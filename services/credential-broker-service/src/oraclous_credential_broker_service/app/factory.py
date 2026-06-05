"""FastAPI app factory (ORAA-4 §21) — build the app, wire routers, no business logic here."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from oraclous_credential_broker_service.routes.credential_routes import router as credential_router
from oraclous_credential_broker_service.routes.internal_routes import router as internal_router
from oraclous_credential_broker_service.services.credential_service import CredentialNotFoundError


def create_app(*, lifespan=None) -> FastAPI:
    app = FastAPI(title="oraclous-credential-broker-service", version="0.0.1", lifespan=lifespan)
    app.include_router(credential_router)
    app.include_router(internal_router)

    @app.exception_handler(CredentialNotFoundError)
    async def _on_not_found(_: Request, exc: CredentialNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy", "service": "credential-broker"}

    return app
