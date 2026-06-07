"""Edge-protection ASGI middleware (ORAA-4 §21 core layer) — request-size guard + rate limit.

Both are pure ASGI (never ``BaseHTTPMiddleware``) so they do not buffer the streaming reverse-proxy
responses and can wrap the inbound ``receive`` channel. They run INSIDE ``RequestIdMiddleware``, so
the ``req_`` id is already on ``scope["state"]`` so every 413/429 carries ``X-Request-Id``.
Each emits the canonical ORA-37 envelope directly via :func:`_send_envelope` (they sit outside the
app's exception handlers).

* ``SizeGuardMiddleware`` — FAIL-CLOSED. 413 ``PAYLOAD_TOO_LARGE`` when the body is not positively
  within the cap: a Content-Length fast-path, then an authoritative byte counter that stops reading
  at ``max+1`` (catches chunked / omitted-length / disagreeing-length). It buffers-then-replays the
  body, matching the proxy (which already calls ``request.body()`` before the upstream), so
  no streaming is lost; reading stops once the cap is crossed, so an oversize upload can never
  buffer beyond the cap.
* ``RateLimitMiddleware`` — FAIL-OPEN. A Redis outage logs (with the request id) and ALLOWS — the
  gateway is the sole ingress, so throttling on a transient Redis blip would self-DoS the platform.
  429 ``RATE_LIMITED`` + ``Retry-After`` when the edge-wide per-client-IP window is exceeded.
"""

from __future__ import annotations

import json
import logging

from oraclous_errors import ErrorCode, build_envelope, new_request_id
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oraclous_application_gateway_service.domain.edge_protection import (
    client_ip,
    content_length_exceeds,
    is_rate_limit_exempt,
)
from oraclous_application_gateway_service.repositories.rate_limit_store import RateLimitStore

logger = logging.getLogger(__name__)


def _request_id(scope: Scope) -> str:
    state = scope.get("state") or {}
    rid = state.get("request_id")
    return rid if isinstance(rid, str) else new_request_id()


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _peer(scope: Scope) -> str | None:
    client = scope.get("client")
    return str(client[0]) if client else None


async def _send_envelope(
    send: Send,
    *,
    status_code: int,
    code: ErrorCode,
    request_id: str,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(build_envelope(code, request_id=request_id), separators=(",", ":")).encode(
        "utf-8"
    )
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("latin-1")),
        (b"x-request-id", request_id.encode("latin-1")),
    ]
    for key, value in (extra_headers or {}).items():
        # ASGI header names are lowercase by convention
        headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class SizeGuardMiddleware:
    """FAIL-CLOSED request-body-size guard — 413 when the body is not positively within the cap."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # cheap fast-path: a declared length already over the cap — reject before reading a chunk
        if content_length_exceeds(_header(scope, b"content-length"), self._max):
            await _send_envelope(
                send,
                status_code=413,
                code=ErrorCode.PAYLOAD_TOO_LARGE,
                request_id=_request_id(scope),
            )
            return

        # authoritative: drain the body, stop at max+1; reject before the app sees oversize
        buffered = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.request":
                buffered += message.get("body", b"")
                if len(buffered) > self._max:
                    await _send_envelope(
                        send,
                        status_code=413,
                        code=ErrorCode.PAYLOAD_TOO_LARGE,
                        request_id=_request_id(scope),
                    )
                    return
                more_body = message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                await self._app(scope, receive, send)
                return

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(buffered), "more_body": False}
            return {"type": "http.disconnect"}

        await self._app(scope, replay_receive, send)


class RateLimitMiddleware:
    """FAIL-OPEN edge-wide rate limit, keyed by client IP (per-key limits are a later slice)."""

    def __init__(
        self, app: ASGIApp, *, limit: int, window_seconds: int, trusted_proxy_count: int
    ) -> None:
        self._app = app
        self._limit = limit
        self._window = window_seconds
        self._trusted_proxy_count = trusted_proxy_count

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_rate_limit_exempt(scope.get("path", "")):
            # liveness + published-contract probes are never throttled (self-DoS guard)
            await self._app(scope, receive, send)
            return

        request_id = _request_id(scope)
        app = scope.get("app")
        redis = getattr(getattr(app, "state", None), "redis", None)
        if redis is None:
            # FAIL-OPEN — never lock the sole ingress on a Redis outage. Not silent: WARN + alert.
            logger.warning(
                "edge rate-limit: redis unavailable, failing open (request_id=%s)", request_id
            )
            await self._app(scope, receive, send)
            return

        ip = client_ip(
            _peer(scope),
            _header(scope, b"x-forwarded-for"),
            trusted_proxy_count=self._trusted_proxy_count,
        )
        try:
            decision = await RateLimitStore(redis).hit(
                ip, limit=self._limit, window_seconds=self._window
            )
        except Exception as exc:  # noqa: BLE001 — fail-open on any redis error (incl. connect)
            logger.error(
                "edge rate-limit: redis error, failing open (request_id=%s): %s", request_id, exc
            )
            await self._app(scope, receive, send)
            return

        if not decision.allowed:
            await _send_envelope(
                send,
                status_code=429,
                code=ErrorCode.RATE_LIMITED,
                request_id=request_id,
                extra_headers={"Retry-After": str(decision.retry_after)},
            )
            return
        await self._app(scope, receive, send)
