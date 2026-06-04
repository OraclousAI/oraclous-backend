"""FastAPI application factory for knowledge-graph-service (ORAA-55).

Creates the R3 HTTP application that exposes graph management and schema
endpoints with API-layer ownership-gate enforcement (T1 / ORAA-55).
"""

from __future__ import annotations

from fastapi import FastAPI

from oraclous_knowledge_graph_service.api.internal.schema import router as internal_schema_router
from oraclous_knowledge_graph_service.api.v1.endpoints.graphs import router as graphs_router


def create_app() -> FastAPI:
    """Build and return the knowledge-graph-service FastAPI app."""
    app = FastAPI(
        title="knowledge-graph-service",
        version="0.0.0",
        description="R3 graph management API with T1 ownership-gate enforcement.",
    )
    app.include_router(graphs_router, prefix="/api/v1")
    app.include_router(internal_schema_router, prefix="/internal/v1")
    return app
