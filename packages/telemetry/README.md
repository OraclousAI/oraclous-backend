# oraclous-telemetry

Shared observability primitives.

## Degradation-alert seam (ADR-021 §2)

A minimal operator-signal primitive: `alert(severity, code, service, detail, **context)` builds a
frozen `DegradationEvent` and fans it out to a list of sinks. The default sink emits a single
structured log record (level routed by severity — `WARNING` / `ERROR` / `CRITICAL`) carrying the
machine `code` so an operator's log pipeline can alert on it.

```python
from oraclous_telemetry import alert, Severity

alert(Severity.ERROR, "store_bind_failed", "auth-service",
      "Postgres unreachable at startup; identity routes disabled", store="postgres")
```

A real alerting backend (Sentry, PagerDuty, an event bus) is wired in later via `register_sink`
without changing any call site. `reset_sinks()` restores just the default log sink (for tests).

It is intentionally not a framework — no batching, no transport, no config. Just the event shape,
one function, and a pluggable sink list.
