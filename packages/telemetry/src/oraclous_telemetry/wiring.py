"""Uniform per-service telemetry wiring (WP-6) — one call mounts correlation + structured logging.

Every service's ``create_app`` calls :func:`install_telemetry(app)` immediately after constructing
the ``FastAPI`` instance. It is a single, uniform addition so the wiring is identical across all
eight services and the ``check_correlation_propagation`` guardrail can assert its presence.
"""

from __future__ import annotations

from typing import Any

from oraclous_telemetry.correlation import CorrelationIdMiddleware
from oraclous_telemetry.logging_config import configure_structured_logging


def install_telemetry(app: Any, *, level: str = "INFO") -> None:
    """Install JSON structured logging + the correlation-id ASGI middleware on ``app``.

    Configures the root logger once (idempotent) and adds :class:`CorrelationIdMiddleware`, which
    binds the inbound (or minted) ``X-Request-Id`` to the logging context for the request. ``app``
    is typed ``Any`` so this shared package carries no ``fastapi``/``starlette`` import in its
    public signature; the call site passes a ``FastAPI`` app.
    """
    configure_structured_logging(level)
    app.add_middleware(CorrelationIdMiddleware)
