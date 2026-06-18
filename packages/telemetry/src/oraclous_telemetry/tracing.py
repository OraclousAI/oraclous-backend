"""OpenTelemetry distributed tracing (#366 part 2) — the shared, gated tracing seam.

Every service already wires the WP-6 correlation-id middleware + JSON structured logging through
:mod:`oraclous_telemetry`. This module adds the *trace* half of that observability story so a single
request can be followed across services in Jaeger, and so a trace joins its logs by the same
correlation id (WP-6).

Three entry points, all consumed through :mod:`oraclous_telemetry`:

* :func:`configure_tracing(service_name)` — install a global ``TracerProvider`` whose ``Resource``
  carries ``service.name``, with a batch ``OTLPSpanExporter`` (gRPC) pointed at
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` (default ``http://jaeger:4317``). Idempotent per process.
* :func:`instrument_app(app)` — instrument a FastAPI app + its outbound clients (httpx, asyncpg,
  and — when the service uses it — neo4j). Called by each service's app factory right after
  ``install_telemetry``.
* :func:`instrument_worker(service_name)` — the Celery-worker mirror: ``configure_tracing`` plus the
  Celery + outbound-client instrumentors. Called from a worker's ``celery_app`` (a worker never runs
  the FastAPI app factory).

THE GATE (HARD RULE — behaviour-neutral when unconfigured). Tracing is a **no-op** unless an OTLP
endpoint is configured: when both ``OTEL_EXPORTER_OTLP_ENDPOINT`` and ``OTEL_ENABLED`` are unset,
every entry point returns immediately — no provider, no exporter, no instrumentation, no behaviour
change — so dev/test/local runs are unaffected. The deployed stack sets
``OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317`` and tracing turns on. (``OTEL_ENABLED=1`` is an
escape hatch to turn tracing on while falling back to the default endpoint.)

WP-6 correlation join. A lightweight :class:`logging`-free span processor stamps the request-bound
``request_id`` (and ``organisation_id`` when bound) from :mod:`oraclous_telemetry.correlation` onto
every span at start, so a trace in Jaeger carries the same ``request_id`` the JSON logs do — logs
and traces join on one id without the handler threading anything.

Layering (ORAA-4 §21): this is a shared package — it imports OpenTelemetry (a declared third-party
dep of :mod:`oraclous_telemetry`) and ``oraclous_telemetry.correlation``, but **no service**. All
OpenTelemetry imports are lazy (inside the functions) so ``import oraclous_telemetry`` never hard-
requires the OTel wheels and the gate short-circuits before any OTel symbol is touched.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from oraclous_telemetry.correlation import get_organisation_id, get_request_id

if TYPE_CHECKING:  # import only for type-checkers; never at runtime (keeps the gate import-free).
    pass

_logger = logging.getLogger("oraclous.tracing")

#: Standard OTel endpoint env var (the primary on/off signal). When set, tracing is enabled.
_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
#: Escape hatch: turn tracing on while falling back to the default endpoint.
_ENABLED_ENV = "OTEL_ENABLED"
#: The default OTLP gRPC endpoint (the Jaeger all-in-one in deploy/docker-compose.yml).
_DEFAULT_ENDPOINT = "http://jaeger:4317"

# Process-level guards so configure/instrument are idempotent (a double-call is a no-op).
_configured = False
_instrumented_app_ids: set[int] = set()
_worker_instrumented = False
# The outbound instrumentors (httpx/asyncpg/neo4j) patch the client libraries PROCESS-GLOBALLY, so
# they are installed exactly once per process — guarded here so building several apps in one process
# (e.g. a test) doesn't re-call them (which the OTel instrumentors only warn-and-no-op about).
_outbound_instrumented = False


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def tracing_enabled() -> bool:
    """The gate: tracing is on only when an OTLP endpoint OR the ``OTEL_ENABLED`` flag is set.

    Behaviour-neutral default: with neither env var present this returns ``False`` and every entry
    point short-circuits — no provider, no exporter, no instrumentation. The deployed stack sets
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` so it returns ``True`` there.
    """
    return bool(os.environ.get(_ENDPOINT_ENV)) or _truthy(os.environ.get(_ENABLED_ENV))


def _endpoint() -> str:
    """The configured OTLP gRPC endpoint, or the Jaeger default when only ``OTEL_ENABLED`` is on."""
    return os.environ.get(_ENDPOINT_ENV) or _DEFAULT_ENDPOINT


def _make_correlation_span_processor() -> Any:
    """Build the WP-6 correlation span processor — a real ``SpanProcessor`` subclass that stamps the
    request-bound ``request_id`` / ``organisation_id`` onto every span at start (so a trace carries
    the same correlation id the JSON logs do; logs↔traces join on one id). on_start runs
    synchronously on the span-opening thread, while the request's contextvars are still bound.

    Subclassing the SDK base — rather than structural typing — is load-bearing: the base supplies
    ``on_ending()`` and any future hooks the provider calls on span end, so a real span lifecycle
    never raises ``AttributeError`` (a structural double missing ``on_ending`` crashed on every span
    end under SDK >= 1.42). The SDK import stays lazy (inside this factory) per the module-scope
    design; exposed at module scope so a test can exercise the real processor against a real
    ``TracerProvider`` span lifecycle (the structural double had hidden the on_ending defect).
    """
    from opentelemetry.sdk.trace import SpanProcessor

    class _CorrelationSpanProcessor(SpanProcessor):
        def on_start(self, span: Any, parent_context: Any = None) -> None:  # noqa: ARG002
            request_id = get_request_id()
            if request_id:
                span.set_attribute("request_id", request_id)
            organisation_id = get_organisation_id()
            if organisation_id:
                span.set_attribute("organisation_id", organisation_id)

        def on_end(self, span: Any) -> None:  # noqa: ARG002 — nothing to flush per span
            return None

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
            return True

    return _CorrelationSpanProcessor()


def configure_tracing(service_name: str) -> bool:
    """Install the global ``TracerProvider`` for ``service_name`` (gRPC OTLP → Jaeger). Idempotent.

    Returns ``True`` when tracing was (or already is) configured, ``False`` when the gate is closed
    (no-op). Sets a ``Resource`` carrying ``service.name`` so spans are attributed to the right
    service in Jaeger, a :class:`_CorrelationSpanProcessor` (the WP-6 join), and a
    ``BatchSpanProcessor`` wrapping an ``OTLPSpanExporter`` (gRPC, ``insecure`` — the local Jaeger
    collector terminates plaintext). Safe to call from every service; the global provider is set
    once per process.
    """
    global _configured
    if not tracing_enabled():
        return False
    if _configured:
        return True

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Respect an existing real provider (e.g. set by a host/operator), only installing ours over the
    # default no-op proxy provider, so we never clobber an out-of-band tracing setup.
    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        _configured = True
        return True

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    # WP-6 correlation join FIRST so request_id is on the span before it is exported.
    provider.add_span_processor(_make_correlation_span_processor())
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=_endpoint(), insecure=True))
    )
    trace.set_tracer_provider(provider)
    _configured = True
    _logger.info("tracing configured: service=%s endpoint=%s", service_name, _endpoint())
    return True


def _instrument_outbound(*, with_neo4j: bool) -> None:
    """Instrument the outbound clients every traced process shares: httpx + asyncpg (+ neo4j).

    Each instrumentor is best-effort; a missing optional instrumentor (there is no neo4j-driver
    instrumentor on the registry — see pyproject) is logged at debug and skipped, never raised —
    instrumentation must never break startup. Process-global + installed once (``_outbound_
    instrumented``): a second app built in the same process reuses the single patch rather than
    re-instrumenting (which the OTel instrumentors only warn-and-no-op about).
    """
    global _outbound_instrumented
    if _outbound_instrumented:
        return
    _outbound_instrumented = True
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001 — instrumentation must never break startup
        _logger.debug("httpx instrumentation skipped: %s", exc)
    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor

        AsyncPGInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        _logger.debug("asyncpg instrumentation skipped: %s", exc)
    if with_neo4j:
        try:
            from opentelemetry.instrumentation.neo4j import Neo4jInstrumentor

            Neo4jInstrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            _logger.debug("neo4j instrumentation skipped: %s", exc)


def instrument_app(app: Any, *, with_neo4j: bool = True) -> bool:
    """Instrument a FastAPI ``app`` + its outbound clients for tracing. Gated + idempotent.

    Adds the ``FastAPIInstrumentor`` (server spans for every request, parented to the inbound
    ``traceparent`` so a gateway→upstream hop is one trace) plus the shared outbound instrumentors
    (httpx, asyncpg, and neo4j when ``with_neo4j``). ``app`` is typed ``Any`` so this shared package
    carries no ``fastapi`` import in its signature. Returns ``True`` when instrumentation ran,
    ``False`` when the gate is closed (no-op). A double-call for the same app is a no-op.

    ``with_neo4j`` defaults ``True`` (the neo4j instrumentor simply no-ops when neo4j is never
    imported, so a non-neo4j service is unaffected); a service can pass ``with_neo4j=False`` to skip
    it explicitly.
    """
    if not configure_tracing(_app_service_name(app)):
        return False
    if id(app) in _instrumented_app_ids:
        return True
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # noqa: BLE001 — never break app startup on an instrumentation hiccup
        _logger.debug("fastapi instrumentation skipped: %s", exc)
    _instrument_outbound(with_neo4j=with_neo4j)
    _instrumented_app_ids.add(id(app))
    return True


def _app_service_name(app: Any) -> str:
    """Best-effort service name for a FastAPI app: its ``title`` (set per service) or a default."""
    title = getattr(app, "title", None)
    return str(title) if title else "oraclous-service"


def instrument_worker(service_name: str, *, with_neo4j: bool = True) -> bool:
    """Instrument a Celery worker process for tracing. Gated + idempotent.

    A worker never runs the FastAPI app factory, so it configures tracing itself and installs the
    ``CeleryInstrumentor`` (a span per task, joined to the publishing request's trace via the
    ``traceparent`` Celery carries in the message headers — see the request-id signal wiring) plus
    the shared outbound instrumentors (httpx, asyncpg, neo4j). Returns ``True`` when it ran,
    ``False`` when the gate is closed (no-op).
    """
    global _worker_instrumented
    if not configure_tracing(service_name):
        return False
    if _worker_instrumented:
        return True
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        _logger.debug("celery instrumentation skipped: %s", exc)
    _instrument_outbound(with_neo4j=with_neo4j)
    _worker_instrumented = True
    return True


def reset_tracing() -> None:
    """Reset the process-level idempotence guards. Tests only — does not tear down a provider."""
    global _configured, _worker_instrumented, _outbound_instrumented
    _configured = False
    _worker_instrumented = False
    _outbound_instrumented = False
    _instrumented_app_ids.clear()
