"""FastAPI application factory for knowledge-retriever-service (ORAA-56, ORAA-60)."""

from __future__ import annotations

from fastapi import FastAPI

from oraclous_knowledge_retriever_service.app.routers import graph, search


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

    app.include_router(search.router)
    app.include_router(graph.router)

    return app
