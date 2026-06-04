"""FastAPI app factory (ORAA-4 §21) — build the app, wire routers, no business logic here."""

from __future__ import annotations

from fastapi import FastAPI


def create_app(*, lifespan=None) -> FastAPI:
    app = FastAPI(
        title="oraclous-credential-broker-service", version="0.0.1", lifespan=lifespan
    )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy", "service": "credential-broker"}

    return app
