"""oraclous-telemetry — shared observability primitives.

Currently exposes the degradation-alert seam (ADR-021 §2): a structured operator signal a service
raises when a critical dependency degrades, backed by structured logging with a pluggable sink for
a real alerting backend later.
"""

from __future__ import annotations

from oraclous_telemetry.degradation import (
    DegradationEvent,
    Severity,
    Sink,
    alert,
    register_sink,
    reset_sinks,
)
from oraclous_telemetry.readiness import (
    EXIT_ON_DEGRADE_ENV,
    STATUS_DEGRADED,
    STATUS_OK,
    ReadinessVerdict,
    evaluate_readiness,
    exit_on_degrade_enabled,
)

__all__ = [
    "EXIT_ON_DEGRADE_ENV",
    "STATUS_DEGRADED",
    "STATUS_OK",
    "DegradationEvent",
    "ReadinessVerdict",
    "Severity",
    "Sink",
    "alert",
    "evaluate_readiness",
    "exit_on_degrade_enabled",
    "register_sink",
    "reset_sinks",
]
