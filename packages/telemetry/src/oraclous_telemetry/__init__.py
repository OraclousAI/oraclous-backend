"""oraclous-telemetry — shared observability primitives.

Currently exposes the degradation-alert seam (ADR-021 §2): a structured operator signal a service
raises when a critical dependency degrades, backed by structured logging with a pluggable sink for
a real alerting backend later.
"""

from __future__ import annotations

from oraclous_telemetry.correlation import (
    REQUEST_ID_HEADER,
    CorrelationIdMiddleware,
    bind_organisation_id,
    bind_request_id,
    get_organisation_id,
    get_request_id,
    new_request_id,
    reset_organisation_id,
    reset_request_id,
)
from oraclous_telemetry.degradation import (
    DegradationEvent,
    Severity,
    Sink,
    alert,
    register_sink,
    reset_sinks,
)
from oraclous_telemetry.logging_config import (
    CorrelationFilter,
    JsonFormatter,
    configure_structured_logging,
    structured_logging_dictconfig,
)
from oraclous_telemetry.readiness import (
    EXIT_ON_DEGRADE_ENV,
    STATUS_DEGRADED,
    STATUS_OK,
    ReadinessVerdict,
    evaluate_readiness,
    exit_on_degrade_enabled,
)
from oraclous_telemetry.wiring import install_telemetry

__all__ = [
    "EXIT_ON_DEGRADE_ENV",
    "REQUEST_ID_HEADER",
    "STATUS_DEGRADED",
    "STATUS_OK",
    "CorrelationFilter",
    "CorrelationIdMiddleware",
    "DegradationEvent",
    "JsonFormatter",
    "ReadinessVerdict",
    "Severity",
    "Sink",
    "alert",
    "bind_organisation_id",
    "bind_request_id",
    "configure_structured_logging",
    "evaluate_readiness",
    "exit_on_degrade_enabled",
    "get_organisation_id",
    "get_request_id",
    "install_telemetry",
    "new_request_id",
    "register_sink",
    "reset_organisation_id",
    "reset_request_id",
    "reset_sinks",
    "structured_logging_dictconfig",
]
