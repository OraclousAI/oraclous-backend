"""FastAPI app factory (ORAA-4 §21) — build the app, wire routers + error envelope, no logic here.

``/health`` + ``/health/upstreams`` are served locally; the catch-all reverse-proxy forwards
everything else. The health router is included FIRST so health is never shadowed. CORS is terminated
once at the edge, and ``RequestIdMiddleware`` mints the correlation id. Every error the gateway
returns — its own and any unhandled exception — is the canonical ORA-37 envelope; an exception body
never leaks a traceback or detail (Interface Contracts §3).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from oraclous_errors import ErrorCode, FieldError, status_to_code
from oraclous_telemetry import configure_structured_logging, instrument_app
from starlette.exceptions import HTTPException as StarletteHTTPException

from oraclous_application_gateway_service.core.agent_cors_middleware import AgentCorsMiddleware
from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.core.edge_middleware import (
    RateLimitMiddleware,
    SizeGuardMiddleware,
)
from oraclous_application_gateway_service.core.middleware import RequestIdMiddleware
from oraclous_application_gateway_service.domain.errors import (
    RouteNotFoundError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from oraclous_application_gateway_service.domain.validation_passthrough import details_from_errors
from oraclous_application_gateway_service.routes.chat_routes import router as chat_router
from oraclous_application_gateway_service.routes.health_routes import router as health_router
from oraclous_application_gateway_service.routes.integration_key_routes import (
    router as integration_key_router,
)
from oraclous_application_gateway_service.routes.mcp_routes import router as mcp_router
from oraclous_application_gateway_service.routes.openapi_routes import router as openapi_router
from oraclous_application_gateway_service.routes.proxy_routes import router as proxy_router
from oraclous_application_gateway_service.routes.published_agent_routes import (
    router as published_agent_router,
)
from oraclous_application_gateway_service.routes.webhook_routes import router as webhook_router
from oraclous_application_gateway_service.schema.error import gateway_error, request_id_of

logger = logging.getLogger(__name__)


def create_app(*, lifespan=None) -> FastAPI:
    # JSON structured logging (WP-6): the gateway's own RequestIdMiddleware binds the minted id to
    # the shared contextvar, so every gateway log line carries the same correlation id it forwards.
    configure_structured_logging()
    settings = get_settings()
    # The published contract is served from routes/openapi_routes (ADR-015), NOT FastAPI's auto-spec
    # (which only sees /health + the catch-all and would leak the `/{path:path}` proxy as an op).
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.VERSION,
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    # Defaults so unit tests that build the app without the lifespan see the degrade paths (redis ->
    # limiter fails open; integration_key_repo -> key auth returns 503) not an AttributeError; the
    # lifespan sets the live clients.
    app.state.redis = None
    app.state.integration_key_repo = None
    app.state.published_agent_repo = None
    app.state.chat_repo = None
    app.state.webhook_subscription_repo = None
    # Starlette runs the LAST-added middleware OUTERMOST, so the runtime order below is
    #   RequestId (outer) -> AgentCors -> CORS -> RateLimit -> SizeGuard -> app.
    # - RequestId outermost: every response (incl. a 413/429 from a guard) carries X-Request-Id.
    # - AgentCors OUTSIDE the gateway-wide CORS (Slice 5): for the published-agent plane only,
    #   it pre-empts the key-less preflight and REPLACES the inner CORS's ACAO with the per-key
    #   decision — so it must wrap CORS (outer) to win on both. A no-op for every other path.
    # - CORS OUTSIDE the guards so (a) a guard's 413/429 still gets Access-Control-Allow-Origin (a
    #   browser can read the RATE_LIMITED/PAYLOAD_TOO_LARGE body), and (b) a preflight OPTIONS is
    #   answered by CORS before the limiter, so preflights don't consume the rate budget.
    # - the guards reject early, before the proxy buffers the body.
    app.add_middleware(SizeGuardMiddleware, max_bytes=settings.MAX_REQUEST_BODY_BYTES)
    app.add_middleware(
        RateLimitMiddleware,
        limit=settings.EDGE_RATE_LIMIT,
        window_seconds=settings.EDGE_RATE_WINDOW_SECONDS,
        trusted_proxy_count=settings.TRUSTED_PROXY_COUNT,
    )
    # allow_credentials=False: the platform authenticates with an `Authorization: Bearer` (the
    # FE never relies on cookies to the gateway), so CORS credentials are not needed — and dropping
    # them makes a permissive origin list SAFE (Starlette emits a literal `ACAO: *`, not the
    # reflect-any-origin-WITH-credentials footgun that `["*"] + credentials` produces). The
    # Bearer token (never auto-sent by the browser) is the gate, not CORS. For defense-in-depth,
    # production SHOULD still pin GATEWAY_CORS_ORIGINS to the real console origin(s).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AgentCorsMiddleware)
    # Outermost: mint the req_ id + set X-Request-Id on every response (success and error).
    app.add_middleware(RequestIdMiddleware)
    # #366: OTel tracing — the inbound server span + the httpx-instrumented proxy hops to upstreams,
    # so a gateway→upstream request is ONE trace in Jaeger. No-op unless OTEL_EXPORTER_OTLP_ENDPOINT
    # is set (the gate); the gateway's own RequestIdMiddleware already binds the WP-6 id the
    # correlation span-processor stamps onto every span.
    instrument_app(app, with_neo4j=False)

    @app.exception_handler(RouteNotFoundError)
    async def _on_route_not_found(request: Request, exc: RouteNotFoundError) -> JSONResponse:
        return gateway_error(request, code=ErrorCode.NOT_FOUND, status_code=404)

    @app.exception_handler(UpstreamUnavailableError)
    async def _on_unavailable(request: Request, exc: UpstreamUnavailableError) -> JSONResponse:
        # 502 Bad Gateway (upstream unreachable); nearest closed-enum code is SERVICE_UNAVAILABLE.
        return gateway_error(request, code=ErrorCode.SERVICE_UNAVAILABLE, status_code=502)

    @app.exception_handler(UpstreamTimeoutError)
    async def _on_timeout(request: Request, exc: UpstreamTimeoutError) -> JSONResponse:
        return gateway_error(request, code=ErrorCode.GATEWAY_TIMEOUT, status_code=504)

    @app.exception_handler(RequestValidationError)
    async def _on_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # The gateway's OWN request-body validation (integration-key mint, published-agent publish,
        # …) — without this handler FastAPI would emit its default ``{"detail": [...]}``, which a
        # typed client that only knows the ORA-37 ``{"error": {...}}`` envelope swallows as a
        # generic INTERNAL_ERROR (#281). Map it onto VALIDATION_FAILED, surfacing ONLY the field
        # path + an error-type machine token (never the value-reflecting ``msg``/``ctx`` — §3 rule
        # 8). If no safe field-level detail is extractable, fall back to one generic detail so the
        # envelope is still the (details-requiring) VALIDATION_FAILED shape, not a detail-free body.
        details = details_from_errors(exc.errors()) or [FieldError(field="body", issue="INVALID")]
        return gateway_error(
            request,
            code=ErrorCode.VALIDATION_FAILED,
            status_code=422,
            message="One or more fields failed validation.",
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _on_http_exc(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Gateway own HTTP errors (401 edge-auth, 405, 503 unavailable, …). Re-attach exc.headers so
        # WWW-Authenticate: Bearer survives on 401; map the status to a closed-enum code.
        return gateway_error(
            request,
            code=status_to_code(exc.status_code),
            status_code=exc.status_code,
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(Exception)
    async def _on_unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak an exception or traceback to the client (§3); log it server-side instead.
        # This handler runs at ServerErrorMiddleware — OUTSIDE RequestIdMiddleware — so the
        # X-Request-Id header is stamped here explicitly (the middleware never wraps this path).
        logger.exception("unhandled gateway error")
        return gateway_error(
            request,
            code=ErrorCode.INTERNAL_ERROR,
            status_code=500,
            headers={"X-Request-Id": request_id_of(request)},
        )

    app.include_router(health_router)
    # the published contract (/v1/openapi.json, /v1/openapi.yaml, /docs) is served at the edge —
    # registered before the catch-all so the proxy never shadows it.
    app.include_router(openapi_router)
    # gateway-local management surfaces (Slice 4) — published agents + integration-key CRUD; before
    # the catch-all so /v1/agents + /v1/integration-keys are served at the edge, not proxied.
    app.include_router(published_agent_router)
    app.include_router(integration_key_router)
    app.include_router(chat_router)  # /v1/chat (member console chat, Slice 6)
    app.include_router(webhook_router)  # /v1/webhooks + /v1/webhook-subscriptions (Slice 7)
    app.include_router(mcp_router)  # /v1/mcp (MCP server — published agents as tools, Slice 8)
    # the proxy catch-all must be LAST so specific routes (e.g. /health, /v1/openapi.json) win
    app.include_router(proxy_router)
    return app
