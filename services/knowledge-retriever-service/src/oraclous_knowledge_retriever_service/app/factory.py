"""Application assembly (app layer) — build FastAPI, include routers, NO handlers."""

from __future__ import annotations

from fastapi import FastAPI
from oraclous_telemetry import install_telemetry, instrument_app

from oraclous_knowledge_retriever_service.core.lifespan import lifespan
from oraclous_knowledge_retriever_service.routes import (
    evaluation_routes,
    federated_routes,
    graph_routes,
    health_routes,
    internal_routes,
    search_routes,
)


def create_app() -> FastAPI:
    app = FastAPI(title="knowledge-retriever-service", version="0.1.0", lifespan=lifespan)
    install_telemetry(app)  # WP-6: JSON structured logging + correlation-id middleware
    instrument_app(app)  # #366: OTel tracing (no-op unless OTEL endpoint set); neo4j-using service
    app.include_router(health_routes.router)
    app.include_router(search_routes.router)
    app.include_router(graph_routes.router)
    app.include_router(evaluation_routes.router)
    app.include_router(federated_routes.router)
    app.include_router(
        internal_routes.router
    )  # core/evaluate — the flow-level judge (ADR-037/#469)
    return app
