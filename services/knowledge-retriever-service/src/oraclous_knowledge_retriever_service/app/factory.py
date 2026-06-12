"""Application assembly (ORAA-4 §21 app layer) — build FastAPI, include routers, NO handlers."""

from __future__ import annotations

from fastapi import FastAPI

from oraclous_knowledge_retriever_service.core.lifespan import lifespan
from oraclous_knowledge_retriever_service.routes import (
    federated_routes,
    graph_routes,
    health_routes,
    search_routes,
)


def create_app() -> FastAPI:
    app = FastAPI(title="knowledge-retriever-service", version="0.1.0", lifespan=lifespan)
    app.include_router(health_routes.router)
    app.include_router(search_routes.router)
    app.include_router(graph_routes.router)
    app.include_router(federated_routes.router)
    return app
