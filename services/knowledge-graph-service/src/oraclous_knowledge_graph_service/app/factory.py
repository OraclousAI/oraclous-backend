"""Application assembly (ORAA-4 §21 app layer) — build FastAPI, include routers, NO handlers.

All request handling lives in `routes/`; all wiring in `core/dependencies` + `core/lifespan`.
This module only composes them.
"""

from __future__ import annotations

from fastapi import FastAPI

from oraclous_knowledge_graph_service.core.lifespan import lifespan
from oraclous_knowledge_graph_service.routes import (
    graph_routes,
    health_routes,
    ingest_routes,
    internal_routes,
    recipe_routes,
)


def create_app() -> FastAPI:
    app = FastAPI(
        title="knowledge-graph-service",
        version="0.3.0",
        lifespan=lifespan,
    )
    app.include_router(health_routes.router)
    app.include_router(graph_routes.router)
    app.include_router(ingest_routes.router)
    app.include_router(internal_routes.router)
    app.include_router(recipe_routes.router)
    return app
