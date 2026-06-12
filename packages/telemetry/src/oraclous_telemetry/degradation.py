"""The shared degradation-alert seam (ADR-021 §2).

A minimal primitive — a function plus an event shape — that lets any service raise a structured
*operator signal* when a critical dependency degrades (a store fails to bind at startup, a
fail-open fallback is taken, …). Today it is backed by structured logging carrying the machine
``code`` + severity; the ``register_sink`` extension point lets a real alerting backend (Sentry,
PagerDuty, an event bus) be wired in later WITHOUT touching any call site.

This is deliberately NOT a framework: no batching, no transport, no config. Just the event shape,
one ``alert(...)`` function, and a pluggable sink list. Layering (ORAA-4 §21): shared package — it
imports no service and holds no business logic; services call ``alert(...)`` from their ``core``
layer (e.g. a lifespan catch site).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger("oraclous.degradation")


class Severity(StrEnum):
    """How loud the signal is. Routes the structured-log level (``WARNING`` vs ``ERROR``).

    ``WARNING`` — degraded but still serving (a fail-open fallback, a non-critical store down).
    ``ERROR`` — a critical dependency is unavailable; the service cannot do its core job.
    ``CRITICAL`` — unrecoverable; the operator must intervene now.
    """

    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# Map a severity to the stdlib log level it emits at. ERROR and CRITICAL both raise above WARNING
# so an operator's log pipeline can alert on level alone, while the machine ``code`` carries the
# precise reason.
_SEVERITY_TO_LOG_LEVEL: dict[Severity, int] = {
    Severity.WARNING: logging.WARNING,
    Severity.ERROR: logging.ERROR,
    Severity.CRITICAL: logging.CRITICAL,
}


@dataclass(frozen=True)
class DegradationEvent:
    """A structured operator signal that a service is running degraded.

    ``code`` is the machine-stable reason (e.g. ``store_bind_failed``) an alert backend or a
    dashboard branches on — it never reflects request content. ``detail`` is the human-readable
    one-liner; ``context`` carries arbitrary structured key/values (the store name, the exception
    string, …) for the backend to index. The shape is frozen so a sink can fan it out safely.
    """

    severity: Severity
    code: str
    service: str
    detail: str
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def log_level(self) -> int:
        """The stdlib logging level this event's severity emits at."""
        return _SEVERITY_TO_LOG_LEVEL[self.severity]


# The pluggable extension point. A sink receives every DegradationEvent. The structured-log sink is
# always present (registered below); a real backend appends itself via register_sink at process
# start. A misbehaving sink must never break the caller — alert() isolates each sink's failure.
Sink = Callable[[DegradationEvent], None]
_sinks: list[Sink] = []


def _log_sink(event: DegradationEvent) -> None:
    """The default sink: emit the event as a single structured log record.

    The machine ``code`` + ``severity`` ride in ``extra`` so a JSON/structured log formatter
    indexes them as fields rather than parsing the message string.
    """
    logger.log(
        event.log_level,
        "degradation: %s — %s",
        event.code,
        event.detail,
        extra={
            "degradation_code": event.code,
            "degradation_severity": str(event.severity),
            "degradation_service": event.service,
            "degradation_context": event.context,
        },
    )


def register_sink(sink: Sink) -> None:
    """Add a sink that receives every subsequent ``alert(...)``. The extension point for a real
    alerting backend. Idempotent for the same callable (a sink is never double-registered)."""
    if sink not in _sinks:
        _sinks.append(sink)


def reset_sinks() -> None:
    """Restore the sink list to just the default structured-log sink. For tests, and for a process
    that wants to drop a previously-registered backend."""
    _sinks.clear()
    _sinks.append(_log_sink)


def alert(
    severity: Severity | str,
    code: str,
    service: str,
    detail: str,
    **context: Any,
) -> DegradationEvent:
    """Raise a structured degradation signal: build the event and fan it out to every sink.

    The default sink writes one structured log record (level = severity, carrying the machine
    ``code``). Returns the built event so a caller can assert on it or attach it to ``app.state``.
    A sink raising never propagates to the caller — degradation reporting must not itself degrade
    the service; a sink failure is logged and the remaining sinks still run.
    """
    event = DegradationEvent(
        severity=Severity(severity),
        code=code,
        service=service,
        detail=detail,
        context=dict(context),
    )
    for sink in _sinks:
        try:
            sink(event)
        except Exception:  # noqa: BLE001 — a broken sink must never break the caller
            logger.exception("degradation sink failed for code=%s", code)
    return event


# Register the always-present structured-log sink at import time.
reset_sinks()
