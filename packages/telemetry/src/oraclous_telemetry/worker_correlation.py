"""Celery request-id threading (#366 part 2, WP-6) — carry the web-path correlation id into workers.

A Celery worker has NO HTTP request, so the WP-6 request-id contextvar is unbound there: a worker's
logs (and spans) would carry no ``request_id`` and could not be joined to the web request that
enqueued the task. This module threads the id across the broker so they CAN:

* on the **publish** side (still in the request, inside :class:`CorrelationIdMiddleware`'s bound
  context), :func:`attach_request_id` copies the bound ``request_id`` into the Celery message
  ``headers`` — the one channel that crosses the broker;
* on the **worker** side, at task start, :func:`bind_request_id_from_headers` re-binds that id to
  the worker process's contextvar, so every log line + span the task emits carries the same id; at
  task end the bind is reset so a pooled worker never leaks one task's id into the next.

These are **celery-free, pure functions** (they take/return plain dicts + tokens) so this shared
package never imports Celery — the thin ``@signal.connect`` wiring that calls them lives in each
worker's ``celery_app`` (knowledge-graph-service, execution-engine-service), the only two services
that run a Celery worker. That keeps Celery out of the seven services that don't use it and makes
the threading logic unit-testable without a broker.

The wire key is ``x-request-id`` (string form) — the same header name the gateway forwards on the
HTTP path (``proxy_service.forward_request_headers``) and :data:`REQUEST_ID_HEADER` carries on the
wire — so the id is one stable name across HTTP hops AND broker hops.
"""

from __future__ import annotations

from typing import Any

from oraclous_telemetry.correlation import bind_request_id, get_request_id, reset_request_id

#: The Celery-message header key carrying the correlation id (string form of ``REQUEST_ID_HEADER``).
#: Same name as the HTTP ``x-request-id`` so logs/traces join on one id across HTTP + broker hops.
REQUEST_ID_HEADER_KEY = "x-request-id"


def attach_request_id(headers: dict[str, Any] | None) -> None:
    """Copy the currently-bound ``request_id`` into a Celery message ``headers`` dict, in place.

    Called from a ``before_task_publish`` signal (still inside the request's bound context). A no-op
    when no id is bound (a task enqueued outside any request — e.g. Celery Beat — simply carries
    none) or when ``headers`` is not a dict, so it never breaks publishing. Never overwrites a
    header already set on the message.
    """
    if not isinstance(headers, dict):
        return
    request_id = get_request_id()
    if request_id and not headers.get(REQUEST_ID_HEADER_KEY):
        headers[REQUEST_ID_HEADER_KEY] = request_id


def request_id_from_headers(headers: Any) -> str:
    """Read the correlation id from a task's request ``headers`` mapping, or ``""`` if absent."""
    if not isinstance(headers, dict):
        return ""
    value = headers.get(REQUEST_ID_HEADER_KEY)
    return value if isinstance(value, str) and value else ""


def bind_request_id_from_headers(headers: Any) -> object | None:
    """Bind the id carried in a task's ``headers`` to the worker context; return the reset token.

    Called from a ``task_prerun`` signal. Returns the reset token (to pass to
    :func:`clear_request_id` in ``task_postrun``) when an id was present, else ``None`` (nothing was
    bound, so nothing to reset).
    """
    request_id = request_id_from_headers(headers)
    if not request_id:
        return None
    return bind_request_id(request_id)


def clear_request_id(token: object | None) -> None:
    """Reset a request-id bind made by :func:`bind_request_id_from_headers`; no-op for ``None``."""
    if token is not None:
        reset_request_id(token)
