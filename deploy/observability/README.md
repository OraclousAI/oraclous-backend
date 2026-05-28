# Observability

Local tracing is provided by the Jaeger all-in-one container in `../docker-compose.yml`.
Services emit OTLP traces directly to `jaeger:4317` (gRPC) / `jaeger:4318` (HTTP); the
Jaeger UI is at http://localhost:16686.

`otel-collector-config.yaml` is a scaffold for a dedicated OpenTelemetry Collector,
introduced when trace volume or multi-backend fan-out warrants it.
