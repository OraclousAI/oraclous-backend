"""FastAPI app factory for capability-registry-service (R2 shell).

Endpoints:

* ``GET /health`` — readiness/liveness probe; returns service status.
* ``GET /api/v1/health`` — v1-prefixed alias for legacy clients.
* ``GET /api/v1/capabilities`` — list capabilities (stub; 501 until R2 impl).
* ``GET /api/v1/capabilities/{capability_id}`` — resolve capability (stub).
* ``POST /api/v1/capabilities`` — register capability (stub).
* ``GET /api/v1/tools`` — list tools (stub; lifts from oraclous-core-service tool registry).
* ``POST /api/v1/tools`` — register tool definition (stub).
* ``GET /api/v1/tools/{tool_id}`` — get tool definition (stub).

Workflow and pipeline routes from oraclous-core-service are retired (ADR-005).
"""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel

from oraclous_capability_registry_service.core.config import get_settings


class _HealthResponse(BaseModel):
    status: str
    version: str


class _CapabilityListResponse(BaseModel):
    capabilities: list[Any]
    total: int


class _ToolListResponse(BaseModel):
    tools: list[Any]
    total: int


def create_app() -> FastAPI:
    """Build the capability-registry-service FastAPI app."""
    settings = get_settings()
    app = FastAPI(title=settings.APP_NAME, version=settings.VERSION)

    # --- GET /health (Kubernetes probe) -----------------------------------

    @app.get("/health", response_model=_HealthResponse)
    async def health() -> _HealthResponse:
        return _HealthResponse(status="healthy", version=settings.VERSION)

    # --- GET /api/v1/health (legacy-compatible alias) ---------------------

    @app.get("/api/v1/health", response_model=_HealthResponse)
    async def api_v1_health() -> _HealthResponse:
        return _HealthResponse(status="healthy", version=settings.VERSION)

    # --- Capability routes (stub — R2 implementation deferred) -----------

    @app.get("/api/v1/capabilities", response_model=_CapabilityListResponse)
    async def list_capabilities() -> _CapabilityListResponse:
        return _CapabilityListResponse(capabilities=[], total=0)

    @app.get("/api/v1/capabilities/{capability_id}")
    async def get_capability(capability_id: str) -> NoReturn:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Capability resolution not yet implemented (R2)",
        )

    @app.post("/api/v1/capabilities", status_code=status.HTTP_201_CREATED)
    async def register_capability() -> NoReturn:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Capability registration not yet implemented (R2)",
        )

    # --- Tool routes (stub — lifts from oraclous-core-service tool registry) ---

    @app.get("/api/v1/tools", response_model=_ToolListResponse)
    async def list_tools() -> _ToolListResponse:
        return _ToolListResponse(tools=[], total=0)

    @app.get("/api/v1/tools/{tool_id}")
    async def get_tool(tool_id: str) -> NoReturn:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Tool resolution not yet implemented (R2)",
        )

    @app.post("/api/v1/tools", status_code=status.HTTP_201_CREATED)
    async def register_tool() -> NoReturn:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Tool registration not yet implemented (R2)",
        )

    return app
