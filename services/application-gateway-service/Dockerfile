# Build context: repo root (required for workspace uv.lock + pyproject.toml).
#
# Build:
#   docker build -f services/application-gateway-service/Dockerfile \
#                -t oraclous-application-gateway-service:dev .
#
# The gateway proxies to the upstream services and, since R6 Slice 3 (ADR-019), owns a small
# Postgres for the integration-key store (alembic migrations run via the gateway-migrate one-shot).

FROM python:3.12.10-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

# Workspace config for lock-file resolution
COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY services/application-gateway-service/pyproject.toml \
     ./services/application-gateway-service/pyproject.toml

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync \
      --package oraclous-application-gateway-service \
      --frozen \
      --no-dev \
      --no-editable

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12.10-slim AS runtime

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY services/application-gateway-service/src/ \
     ./services/application-gateway-service/src/
# the published public contract (ADR-015), served at /v1/openapi.json + /docs
COPY openapi/ ./openapi/
# the integration-key store migrations (R6 Slice 3, ADR-019) — `alembic upgrade head` runs from the
# service WORKDIR via the gateway-migrate one-shot
COPY services/application-gateway-service/alembic.ini \
     ./services/application-gateway-service/alembic.ini
COPY services/application-gateway-service/migrations/ \
     ./services/application-gateway-service/migrations/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/application-gateway-service/src" \
    OPENAPI_SPEC_PATH="/app/openapi/v1.yaml"
WORKDIR /app/services/application-gateway-service

EXPOSE 8000

RUN addgroup --system svc && adduser --system --ingroup svc --no-create-home svc
RUN chown -R svc:svc /app
USER svc

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "oraclous_application_gateway_service.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]
