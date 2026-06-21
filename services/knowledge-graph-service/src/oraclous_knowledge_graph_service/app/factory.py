"""Application assembly (app layer) — build FastAPI, include routers, NO handlers.

All request handling lives in `routes/`; all wiring in `core/dependencies` + `core/lifespan`.
This module only composes them.
"""

from __future__ import annotations

from fastapi import FastAPI
from oraclous_telemetry import install_telemetry, instrument_app

from oraclous_knowledge_graph_service.core.lifespan import lifespan
from oraclous_knowledge_graph_service.routes import (
    community_routes,
    graph_routes,
    health_routes,
    ingest_routes,
    internal_routes,
    memory_routes,
    ontology_routes,
    recipe_routes,
    resolution_routes,
)


def create_app() -> FastAPI:
    app = FastAPI(
        title="knowledge-graph-service",
        version="0.4.0",
        lifespan=lifespan,
    )
    install_telemetry(app)  # WP-6: JSON structured logging + correlation-id middleware
    instrument_app(app)  # #366: OTel tracing (no-op unless OTEL endpoint set); neo4j-using service
    app.include_router(health_routes.router)
    app.include_router(graph_routes.router)
    app.include_router(resolution_routes.router)
    app.include_router(ingest_routes.router)
    app.include_router(internal_routes.router)
    app.include_router(recipe_routes.router)
    app.include_router(ontology_routes.router)
    app.include_router(ontology_routes.suggest_router)
    app.include_router(community_routes.kinds_router)
    app.include_router(community_routes.router)
    app.include_router(memory_routes.router)
    return app
