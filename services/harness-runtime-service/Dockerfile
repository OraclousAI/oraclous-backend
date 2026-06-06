# Build context: repo root (required for the workspace uv.lock + pyproject.toml).
#
# Build:
#   docker build -f services/harness-runtime-service/Dockerfile \
#                -t oraclous-harness-runtime-service:dev .

FROM python:3.12.10-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY services/harness-runtime-service/pyproject.toml \
     ./services/harness-runtime-service/pyproject.toml

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync \
      --package oraclous-harness-runtime-service \
      --frozen \
      --no-dev \
      --no-editable

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12.10-slim AS runtime

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY services/harness-runtime-service/src/ \
     ./services/harness-runtime-service/src/
COPY services/harness-runtime-service/alembic.ini \
     ./services/harness-runtime-service/alembic.ini
COPY services/harness-runtime-service/migrations/ \
     ./services/harness-runtime-service/migrations/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/harness-runtime-service/src"
WORKDIR /app/services/harness-runtime-service

RUN addgroup --system svc && adduser --system --ingroup svc --no-create-home svc
RUN chown -R svc:svc /app
USER svc

EXPOSE 8000
CMD ["uvicorn", "oraclous_harness_runtime_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
