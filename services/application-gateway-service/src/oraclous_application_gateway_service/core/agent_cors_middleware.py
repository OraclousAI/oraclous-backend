"""Per-key CORS for the published-agent public plane (ORAA-4 §21 core layer).

A path-scoped pure-ASGI middleware (never ``BaseHTTPMiddleware`` — the streaming proxy must not be
buffered) that sits OUTSIDE the gateway-wide ``CORSMiddleware`` (added after it → wraps it). For
``/v1/agents/{slug}[/invoke]`` ONLY it (1) answers the key-less preflight directly (short-circuiting
before the inner CORS, so the wildcard/static CORS never also answers it) and (2) on the keyed
response REPLACES whatever ACAO the inner CORS set with the per-key decision (the resolved key's
``cors_origins`` on ``scope["state"]``). It is a no-op on every other path — the gateway-wide CORS
stays fully authoritative for the proxied + management routes.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oraclous_application_gateway_service.domain.cors_policy import (
    is_public_agent_path,
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
            # preflight: the key is unknown (no Authorization) — answer permissively + short-circuit
            await self._send_preflight(send, origin, headers.get(b"access-control-request-headers"))
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
