"""FastAPI app factory (ORAA-4 §21) — build the app, wire routers + error envelope, no logic here.

``/health`` + ``/health/upstreams`` are served locally; the catch-all reverse-proxy forwards
everything else. The health router is included FIRST so health is never shadowed. CORS is terminated
once at the edge. The gateway's OWN errors (401/404/502/503/504) are returned as the
forward-compatible error envelope; upstream errors pass through verbatim.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.domain.errors import (
    RouteNotFoundError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from oraclous_application_gateway_service.routes.health_routes import router as health_router
from oraclous_application_gateway_service.routes.proxy_routes import router as proxy_router
from oraclous_application_gateway_service.schema.error import gateway_error


def create_app(*, lifespan=None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.APP_NAME, version=settings.VERSION, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RouteNotFoundError)
    async def _on_route_not_found(request: Request, exc: RouteNotFoundError) -> JSONResponse:
        return gateway_error(
            request,
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="route_not_found",
            message=f"no upstream route for {request.url.path}",
        )

    @app.exception_handler(UpstreamUnavailableError)
    async def _on_unavailable(request: Request, exc: UpstreamUnavailableError) -> JSONResponse:
        return gateway_error(
            request,
            status_code=status.HTTP_502_BAD_GATEWAY,
            error_code="upstream_unavailable",
            message="the upstream could not be reached",
        )

    @app.exception_handler(UpstreamTimeoutError)
    async def _on_timeout(request: Request, exc: UpstreamTimeoutError) -> JSONResponse:
        return gateway_error(
            request,
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            error_code="upstream_timeout",
            message="the upstream did not respond in time",
        )

    @app.exception_handler(StarletteHTTPException)
    async def _on_http_exc(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # the gateway's own HTTP errors (401 edge-auth, 503 unavailable, …) → envelope
        return gateway_error(
            request,
            status_code=exc.status_code,
            error_code=f"http_{exc.status_code}",
            message=str(exc.detail),
        )

    app.include_router(health_router)
    # the proxy catch-all must be LAST so specific routes (e.g. /health) win
    app.include_router(proxy_router)
    return app
