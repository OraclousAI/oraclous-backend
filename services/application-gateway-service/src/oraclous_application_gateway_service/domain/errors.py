"""Gateway domain errors (ORAA-4 §21 domain layer)."""

from __future__ import annotations


class RouteNotFoundError(Exception):
    """No upstream prefix matches the request path — maps to HTTP 404 (closed allow-list)."""


class UpstreamUnavailableError(Exception):
    """The upstream could not be connected to — maps to HTTP 502 Bad Gateway."""


class UpstreamTimeoutError(Exception):
    """The upstream did not respond within the timeout — maps to HTTP 504 Gateway Timeout."""
