"""Per-key CORS for the published-agent public plane (core layer).

A path-scoped pure-ASGI middleware (never ``BaseHTTPMiddleware`` — the streaming proxy must not be
buffered) that sits OUTSIDE the gateway-wide ``CORSMiddleware`` (added after it → wraps it). For
``/v1/agents/{slug}[/invoke]`` ONLY it (1) answers the key-less **public-plane** preflight directly
(short-circuiting before the inner CORS, so the wildcard/static CORS never also answers it) and (2)
on the keyed response REPLACES whatever ACAO the inner CORS set with the per-key decision (the
resolved key's ``cors_origins`` on ``scope["state"]``). It is a no-op on every other path — the
gateway-wide CORS stays fully authoritative for the proxied + management routes.

The path is SHARED with the member plane (``DELETE /v1/agents/{slug}`` = admin unpublish): BOTH its
preflight AND its actual response defer to the gateway-wide CORS. A preflight whose
``Access-Control-Request-Method`` is not a public-plane method (GET/POST) is not owned here; an
actual request whose method is not public-plane (DELETE) is likewise passed through WITHOUT the
per-key rewrite. So the inner gateway-wide CORS (``allow_methods=["*"]`` + console origins) sets the
ACAO on both. Without the actual-response deferral the rewrite strips the inner ACAO off the DELETE
response (no resolved key → ``cors=None`` → fail-closed) and the browser blocks the read even though
the server returned 204 (#289).
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oraclous_application_gateway_service.domain.cors_policy import (
    is_public_agent_path,
    is_public_plane_method,
    is_public_plane_preflight,
    preflight_headers,
    rewrite_response_headers,
)


class AgentCorsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_public_agent_path(scope.get("path", "")):
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        origin = headers.get(b"origin")
        if origin is None:  # not a browser cross-origin request — nothing to manage
            await self._app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            # Preflight. AgentCors only OWNS the PUBLIC plane (GET metadata, POST invoke) on this
            # shared path. A member-plane preflight (DELETE = admin unpublish, #289) must NOT get
            # the public-plane policy (it omits DELETE + the console origin) — defer it to the inner
            # gateway-wide CORS (allow_methods=["*"] + the console origins), which answers it.
            if not is_public_plane_preflight(headers.get(b"access-control-request-method")):
                await self._app(scope, receive, send)
                return
            # public-plane preflight: the key is unknown (no Authorization) — answer permissively
            await self._send_preflight(send, origin, headers.get(b"access-control-request-headers"))
            return

        # Actual (non-preflight) request. AgentCors only OWNS the PUBLIC-plane (GET/POST) response —
        # it rewrites the inner CORS's ACAO to the per-key decision. A member-plane actual request
        # (DELETE = admin unpublish, #289) carries a member JWT, not a bound key → no resolved_key →
        # the rewrite would STRIP the gateway-wide ACAO (cors=None is fail-closed) and void the
        # browser read. Defer to the gateway CORS, which already set the console-origin ACAO.
        if not is_public_plane_method(scope.get("method")):
            await self._app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                resolved = (scope.get("state") or {}).get("resolved_key")
                cors = resolved.cors_origins if resolved is not None else None
                message = {
                    **message,
                    "headers": rewrite_response_headers(message.get("headers") or [], origin, cors),
                }
            await send(message)

        await self._app(scope, receive, send_wrapper)

    @staticmethod
    async def _send_preflight(send: Send, origin: bytes, request_headers: bytes | None) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": preflight_headers(origin, request_headers),
            }
        )
        await send({"type": "http.response.body", "body": b""})
