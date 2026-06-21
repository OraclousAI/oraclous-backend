"""End-to-end correlation id — the contextvar + the ASGI middleware (WP-6, A5).

One shared implementation, consumed by every service. The gateway mints a server-authoritative
``req_`` request id at the edge (``oraclous_errors.new_request_id``) and forwards it as
``X-Request-Id`` to its upstream (gateway ``proxy_service.forward_request_headers``). Each upstream
service mounts :class:`CorrelationIdMiddleware`, which:

* reads the inbound ``X-Request-Id`` (the gateway's id) — or mints one if absent, so a *direct*
  call (a service reached by host IP:port before the gateway exists, or a sibling-service call)
  still has a correlation handle;
* binds it to a :class:`contextvars.ContextVar` so the structured-logging filter
  (:mod:`oraclous_telemetry.logging_config`) injects it into every log record emitted while the
  request is handled — without the handler having to thread it through;
* re-emits it on the response ``X-Request-Id`` header so a client/operator sees the same id end to
  end.

It is pure ASGI (not ``BaseHTTPMiddleware``) so it never buffers a streaming response body, and it
strips any *inbound* response-shaped echo: the value re-emitted downstream is always the one bound
for this request, never a client-forgeable second copy.

The ``organisation_id`` contextvar is bound separately by whatever resolves the org for a request
(the trusted ``X-Organisation-Id`` edge header, or a token claim) — see ``bind_organisation_id``.
The middleware does not assume where org context comes from; it only owns the request id.
"""

from __future__ import annotations

import secrets
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: The wire header name (lower-cased, as ASGI delivers it).
REQUEST_ID_HEADER = b"x-request-id"

#: Bound for the lifetime of a request; read by the logging filter. Empty string = unbound.
_request_id_var: ContextVar[str] = ContextVar("oraclous_request_id", default="")
_organisation_id_var: ContextVar[str] = ContextVar("oraclous_organisation_id", default="")


def new_request_id() -> str:
    """Mint an opaque correlation id matching the platform ``^req_[0-9A-Za-z]+$`` shape.

    Kept local to ``oraclous_telemetry`` so the shared middleware carries no cross-package import;
    it mirrors ``oraclous_errors.new_request_id`` (the gateway minter) on purpose.
    """
    return "req_" + secrets.token_hex(16)


def get_request_id() -> str:
    """The request id bound for the current request, or ``""`` if none is bound."""
    return _request_id_var.get()


def bind_request_id(request_id: str) -> object:
    """Bind ``request_id`` to the current context; return the reset token."""
    return _request_id_var.set(request_id)


def reset_request_id(token: object) -> None:
    """Reset the request-id contextvar using the token from :func:`bind_request_id`."""
    _request_id_var.reset(token)  # type: ignore[arg-type]


def get_organisation_id() -> str:
    """The organisation id bound for the current request, or ``""`` if none is bound."""
    return _organisation_id_var.get()


def bind_organisation_id(organisation_id: str) -> object:
    """Bind ``organisation_id`` to the current context; return the reset token.

    Called by whatever resolves the org for a request so the structured-logging filter can stamp it
    onto every record. Fail-soft: an empty/None value binds ``""`` (the field is simply absent).
    """
    return _organisation_id_var.set(organisation_id or "")


def reset_organisation_id(token: object) -> None:
    """Reset the organisation-id contextvar using the token from :func:`bind_organisation_id`."""
    _organisation_id_var.reset(token)  # type: ignore[arg-type]


def _inbound_request_id(scope: Scope) -> str:
    """Read the inbound ``X-Request-Id`` from the ASGI scope, or mint one if absent/blank."""
    for key, value in scope.get("headers", []):
        if key.lower() == REQUEST_ID_HEADER:
            decoded = value.decode("latin-1").strip()
            if decoded:
                return decoded
    return new_request_id()


class CorrelationIdMiddleware:
    """Bind the inbound (or freshly-minted) request id to the context + re-emit it downstream.

    Pure ASGI so it never buffers streaming responses. Binds the request-id contextvar for the
    duration of the request and resets it after, so a pooled worker never leaks one request's id
    into the next. Re-emits the bound id on the response ``X-Request-Id`` header (stripping any echo
    the app already set, so exactly one is returned).
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = _inbound_request_id(scope)
        token = bind_request_id(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != REQUEST_ID_HEADER
                ]
                headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        finally:
            reset_request_id(token)
