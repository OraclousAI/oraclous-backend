"""Pure-ASGI correlation-id middleware (ORAA-4 §21 core layer).

Mints a server-authoritative ``req_`` request id at the edge of every request,
stashes it on ``scope["state"]`` (readable as ``request.state.request_id`` by the
error handlers), and sets the ``X-Request-Id`` response header on every response it
wraps — streamed or buffered. (The catch-all 500 handler runs at ServerErrorMiddleware,
outside this middleware, and stamps the header itself.) It is pure ASGI rather than
``BaseHTTPMiddleware`` so it never buffers the streaming reverse-proxy responses,
and it strips any client-supplied ``x-request-id`` so a caller cannot forge the
correlation handle.
"""

from __future__ import annotations

from oraclous_errors import new_request_id
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_HEADER = b"x-request-id"


class RequestIdMiddleware:
    """Mint and propagate the server-authoritative request id."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != _HEADER
                ]
                headers.append((_HEADER, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_request_id)
