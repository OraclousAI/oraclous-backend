"""FastAPI application factory for knowledge-retriever-service (ORAA-56)."""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build and return the knowledge-retriever-service FastAPI app."""
    app = FastAPI(
        title="knowledge-retriever-service",
        version="0.0.0",
        description="R3 canonical retrieval API.",
    )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy", "version": "0.0.0"}

    return app
