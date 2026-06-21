"""Readiness-reflecting health helper (ADR-021 startup-degradation policy).

Each substrate/runtime service binds its critical store(s) into ``app.state`` at startup; a
store-bind failure leaves the attribute ``None`` (degrade-don't-crash). This helper turns that
state into a uniform health verdict so every service's ``/health`` + ``/readyz`` routes stay thin
(no DB access in routes) and consistent.

Policy (ADR-021):
* ``/health`` is **liveness** — always HTTP 200; the *body* reflects ``ok`` vs ``degraded`` so an
  operator/dashboard sees the degradation without the container being killed.
* ``/readyz`` is **readiness** — HTTP 503 when degraded (so an orchestrator/load-balancer stops
  routing traffic), 200 when ok.

A "critical store" is one whose absence means the service cannot do its core job. Non-critical
fail-open dependencies (e.g. a rate-limiter's Redis) are NOT passed here — their degradation is
still alerted at the lifespan catch site, but it does not flip readiness.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from http import HTTPStatus

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"

# The flag-gated crash-on-degrade env var (ADR-021). Default OFF: a degraded startup serves a
# degraded body and never exits, so dev/CI (no Postgres/Neo4j) do not crash-loop. An operator sets
# this true in a managed deploy where the orchestrator should restart a service that came up
# without its critical store. Truthy values: 1/true/yes/on (case-insensitive).
EXIT_ON_DEGRADE_ENV = "EXIT_ON_STARTUP_DEGRADE"
_TRUTHY = {"1", "true", "yes", "on"}


def exit_on_degrade_enabled() -> bool:
    """Whether the flag-gated crash-on-degrade behaviour is enabled (default OFF)."""
    return os.environ.get(EXIT_ON_DEGRADE_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ReadinessVerdict:
    """The computed health verdict for a service.

    ``status`` is ``ok`` | ``degraded``; ``degraded_stores`` names the critical stores that failed
    to bind (empty when ok); ``readyz_status_code`` is the HTTP code the readiness path returns
    (200 ok / 503 degraded). Liveness always returns 200 regardless.
    """

    status: str
    degraded_stores: tuple[str, ...]

    @property
    def is_degraded(self) -> bool:
        return self.status == STATUS_DEGRADED

    @property
    def readyz_status_code(self) -> int:
        return HTTPStatus.SERVICE_UNAVAILABLE if self.is_degraded else HTTPStatus.OK


def evaluate_readiness(critical_stores: dict[str, object | None]) -> ReadinessVerdict:
    """Map ``{store_name: bound_object_or_None}`` to a verdict.

    A store whose value is ``None`` did not bind → ``degraded``; all present → ``ok``. The
    store-name ordering is preserved so the body is stable for assertions/dashboards.
    """
    degraded = tuple(name for name, value in critical_stores.items() if value is None)
    status = STATUS_DEGRADED if degraded else STATUS_OK
    return ReadinessVerdict(status=status, degraded_stores=degraded)
