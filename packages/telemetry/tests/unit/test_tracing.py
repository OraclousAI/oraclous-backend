"""Unit tests for the OTel tracing seam (#366 part 2) — the GATE, idempotence, the WP-6 join.

The single hardest invariant is the gate: with neither ``OTEL_EXPORTER_OTLP_ENDPOINT`` nor
``OTEL_ENABLED`` set, every entry point is a behaviour-neutral no-op (no provider, no exporter, no
instrumentation) — so dev/test/local runs are unaffected. These tests pin that, the idempotence
guards, and that the correlation span-processor stamps the WP-6 request id onto a span.
"""

from __future__ import annotations

import pytest
from oraclous_telemetry import (
    configure_tracing,
    instrument_app,
    instrument_worker,
    reset_tracing,
    tracing_enabled,
)
from oraclous_telemetry.correlation import (
    bind_organisation_id,
    bind_request_id,
    reset_organisation_id,
    reset_request_id,
)
from oraclous_telemetry.tracing import (
    _ENABLED_ENV,
    _ENDPOINT_ENV,
    _make_correlation_span_processor,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_tracing_env(monkeypatch: pytest.MonkeyPatch):
    """Each test starts from the closed gate + fresh idempotence guards."""
    monkeypatch.delenv(_ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(_ENABLED_ENV, raising=False)
    reset_tracing()
    yield
    reset_tracing()


# --- the gate ------------------------------------------------------------------------------------


def test_gate_closed_by_default():
    assert tracing_enabled() is False


def test_gate_opens_on_endpoint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(_ENDPOINT_ENV, "http://jaeger:4317")
    assert tracing_enabled() is True


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_gate_opens_on_enabled_flag(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv(_ENABLED_ENV, value)
    assert tracing_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
def test_enabled_flag_falsy_keeps_gate_closed(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv(_ENABLED_ENV, value)
    assert tracing_enabled() is False


def test_configure_tracing_is_noop_when_gated():
    # Returns False (did nothing) and installs no real provider.
    assert configure_tracing("svc-x") is False
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    assert not isinstance(trace.get_tracer_provider(), TracerProvider)


def test_instrument_app_is_noop_when_gated():
    class _FakeApp:
        title = "svc-y"

    # No FastAPI needed: the gate short-circuits before any instrumentor import.
    assert instrument_app(_FakeApp()) is False


def test_instrument_worker_is_noop_when_gated():
    assert instrument_worker("svc-z") is False


# --- enabled path: provider install + idempotence ------------------------------------------------


def test_configure_tracing_installs_provider_when_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(_ENDPOINT_ENV, "http://jaeger:4317")
    assert configure_tracing("svc-real") is True

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # service.name lands on the Resource so spans attribute to the right service in Jaeger.
    assert provider.resource.attributes.get("service.name") == "svc-real"


def test_configure_tracing_is_idempotent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(_ENABLED_ENV, "1")
    assert configure_tracing("svc-a") is True
    # A second call (even with a different name) is a no-op that still reports configured.
    assert configure_tracing("svc-a") is True


def test_instrument_app_idempotent_per_app(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI

    monkeypatch.setenv(_ENABLED_ENV, "1")
    app = FastAPI(title="svc-app")
    assert instrument_app(app, with_neo4j=False) is True
    # Second call for the same app short-circuits (no double-instrumentation) but still True.
    assert instrument_app(app, with_neo4j=False) is True


# --- the WP-6 correlation join (request_id/org_id stamped onto a span) ---------------------------


def _local_provider_with_correlation():
    """A real, NON-GLOBAL TracerProvider carrying the WP-6 correlation processor + an in-memory
    exporter, so a test can drive a real span START→END. The old structural double only ever had
    on_start called, which is exactly why the on_ending crash on span end (SDK >= 1.42) slipped
    through — these tests exercise the full lifecycle the deployed provider does."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(_make_correlation_span_processor())  # WP-6 join (on_start)
    provider.add_span_processor(SimpleSpanProcessor(exporter))  # capture finished spans
    return provider, exporter


def test_correlation_processor_stamps_ids_over_a_real_span_lifecycle():
    # Real provider + real span START→END: exercises on_start (stamp) AND the on_ending/on_end hooks
    # the SDK calls on span end — regression guard for the structural-double crash (SDK >= 1.42).
    provider, exporter = _local_provider_with_correlation()
    rid_token = bind_request_id("req_abc123")
    org_token = bind_organisation_id("org-7")
    try:
        with provider.get_tracer("test").start_as_current_span("op"):
            pass  # span ends here — must not raise
    finally:
        reset_organisation_id(org_token)
        reset_request_id(rid_token)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs["request_id"] == "req_abc123"
    assert attrs["organisation_id"] == "org-7"


def test_correlation_processor_omits_unbound_ids_over_a_real_span_lifecycle():
    # No request/org bound → nothing stamped (no empty-string noise on the span).
    provider, exporter = _local_provider_with_correlation()
    with provider.get_tracer("test").start_as_current_span("op"):
        pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert "request_id" not in attrs
    assert "organisation_id" not in attrs


def test_real_span_end_does_not_raise():
    # Explicit regression guard: a structural double missing on_ending raised AttributeError on
    # span end (SDK >= 1.42); the real SpanProcessor subclass inherits on_ending, so end is safe.
    provider, _ = _local_provider_with_correlation()
    span = provider.get_tracer("test").start_span("op")
    span.end()  # would AttributeError with the old structural processor
