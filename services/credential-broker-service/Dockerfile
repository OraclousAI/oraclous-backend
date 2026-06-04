# Build context: repo root (required for workspace uv.lock + pyproject.toml).
#
# Build:
#   docker build -f services/credential-broker-service/Dockerfile -t oraclous-credential-broker-service:dev .

FROM python:3.12.10-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY services/credential-broker-service/pyproject.toml \
     ./services/credential-broker-service/pyproject.toml

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync \
      --package oraclous-credential-broker-service \
      --frozen \
      --no-dev \
      --no-editable

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12.10-slim AS runtime

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

COPY services/credential-broker-service/src/ ./services/credential-broker-service/src/
COPY services/credential-broker-service/migrations/ ./services/credential-broker-service/migrations/
COPY services/credential-broker-service/alembic.ini ./services/credential-broker-service/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/credential-broker-service/src"
WORKDIR /app/services/credential-broker-service

RUN addgroup --system svc && adduser --system --ingroup svc --no-create-home svc
RUN chown -R svc:svc /app
USER svc

# Default: serve the API. The compose one-shot overrides this to run `alembic upgrade head`.
EXPOSE 8000
CMD ["uvicorn", "oraclous_credential_broker_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
