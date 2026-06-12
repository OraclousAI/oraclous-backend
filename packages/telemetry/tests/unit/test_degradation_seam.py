"""Unit tests for the degradation-alert seam (ADR-021 §2, ORAA-297).

Pins three things the seam guarantees:

* the event *shape* — fields, frozen, severity-coerced-from-str, log-level routing;
* the structured-log *emission* — one record at the right level carrying the machine ``code`` +
  severity in ``extra``;
* severity *routing* — WARNING vs ERROR vs CRITICAL map to the right stdlib levels;

plus the pluggable-sink extension point (register/reset, fan-out, and a broken sink never breaks
the caller).
"""

from __future__ import annotations

import logging

import pytest
from oraclous_telemetry import (
    DegradationEvent,
    Severity,
    alert,
    register_sink,
    reset_sinks,
)
from oraclous_telemetry.degradation import _log_sink


@pytest.fixture(autouse=True)
def _isolate_sinks():
    """Each test starts from just the default log sink, and leaves it that way."""
    reset_sinks()
    yield
    reset_sinks()


# --- event shape ------------------------------------------------------------


def test_event_shape_carries_all_fields():
    event = alert(
        Severity.ERROR,
        "store_bind_failed",
        "auth-service",
        "Postgres unreachable at startup",
        store="postgres",
        attempt=2,
    )
    assert isinstance(event, DegradationEvent)
    assert event.severity is Severity.ERROR
    assert event.code == "store_bind_failed"
    assert event.service == "auth-service"
    assert event.detail == "Postgres unreachable at startup"
    assert event.context == {"store": "postgres", "attempt": 2}


def test_event_is_frozen():
    event = alert(Severity.WARNING, "c", "svc", "d")
    with pytest.raises(Exception):  # noqa: B017,PT011 — dataclass FrozenInstanceError
        event.code = "mutated"  # type: ignore[misc]


def test_severity_accepts_string():
    event = alert("error", "c", "svc", "d")
    assert event.severity is Severity.ERROR


def test_invalid_severity_string_rejected():
    with pytest.raises(ValueError):
        alert("loud", "c", "svc", "d")


def test_context_defaults_empty():
    event = alert(Severity.WARNING, "c", "svc", "d")
    assert event.context == {}


# --- severity → log-level routing ------------------------------------------


@pytest.mark.parametrize(
    ("severity", "level"),
    [
        (Severity.WARNING, logging.WARNING),
        (Severity.ERROR, logging.ERROR),
        (Severity.CRITICAL, logging.CRITICAL),
    ],
)
def test_log_level_routing(severity: Severity, level: int):
    event = DegradationEvent(severity=severity, code="c", service="svc", detail="d")
    assert event.log_level == level


def test_alert_emits_at_severity_level(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="oraclous.degradation"):
        alert(Severity.ERROR, "store_bind_failed", "auth-service", "down")
    records = [r for r in caplog.records if r.name == "oraclous.degradation"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


def test_warning_does_not_emit_at_error_level(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.DEBUG, logger="oraclous.degradation"):
        alert(Severity.WARNING, "fail_open", "auth-service", "rate limiter open")
    record = next(r for r in caplog.records if r.name == "oraclous.degradation")
    assert record.levelno == logging.WARNING


# --- structured-log emission carries the machine code -----------------------


def test_structured_log_carries_code_and_severity(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="oraclous.degradation"):
        alert(
            Severity.ERROR,
            "store_bind_failed",
            "knowledge-graph-service",
            "Neo4j unreachable",
            store="neo4j",
        )
    record = next(r for r in caplog.records if r.name == "oraclous.degradation")
    assert record.degradation_code == "store_bind_failed"
    assert record.degradation_severity == "error"
    assert record.degradation_service == "knowledge-graph-service"
    assert record.degradation_context == {"store": "neo4j"}
    # the machine code appears in the rendered message too
    assert "store_bind_failed" in record.getMessage()


# --- pluggable sink extension point ----------------------------------------


def test_registered_sink_receives_event():
    received: list[DegradationEvent] = []
    register_sink(received.append)
    event = alert(Severity.ERROR, "c", "svc", "d")
    assert received == [event]


def test_register_sink_is_idempotent():
    received: list[DegradationEvent] = []
    register_sink(received.append)
    register_sink(received.append)
    alert(Severity.WARNING, "c", "svc", "d")
    assert len(received) == 1


def test_reset_sinks_restores_only_log_sink():
    received: list[DegradationEvent] = []
    register_sink(received.append)
    reset_sinks()
    alert(Severity.WARNING, "c", "svc", "d")
    assert received == []


def test_broken_sink_does_not_break_caller():
    good: list[DegradationEvent] = []

    def _boom(_: DegradationEvent) -> None:
        raise RuntimeError("sink exploded")

    register_sink(_boom)
    register_sink(good.append)
    event = alert(Severity.ERROR, "c", "svc", "d")  # must not raise
    assert good == [event]


def test_default_log_sink_is_present_after_reset():
    from oraclous_telemetry.degradation import _sinks

    reset_sinks()
    assert _log_sink in _sinks
