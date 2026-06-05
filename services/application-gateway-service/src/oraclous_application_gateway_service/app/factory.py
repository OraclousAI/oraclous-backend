"""FastAPI app factory (ORAA-4 §21) — build the app, wire routers, no business logic here.

``/health`` is served locally; the catch-all reverse-proxy forwards everything else to its upstream.
The health router is included FIRST so ``/health`` is never shadowed by the proxy catch-all. CORS is
terminated once at the edge (so upstreams don't each carry it); upstream-health aggregation is added
in GW-5.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.routes.health_routes import router as health_router
from oraclous_application_gateway_service.routes.proxy_routes import router as proxy_router


def create_app(*, lifespan=None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.APP_NAME, version=settings.VERSION, lifespan=lifespan)
    # CORS terminated at the edge (preflight + headers handled once for every upstream).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    # the proxy catch-all must be LAST so specific routes (e.g. /health) win
    app.include_router(proxy_router)
    return app
