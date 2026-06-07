# Build context: repo root (required for the workspace uv.lock + pyproject.toml).
#
# Build:
#   docker build -f services/execution-engine-service/Dockerfile \
#                -t oraclous-execution-engine-service:dev .

FROM python:3.12.10-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY services/execution-engine-service/pyproject.toml \
     ./services/execution-engine-service/pyproject.toml

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync \
      --package oraclous-execution-engine-service \
      --frozen \
      --no-dev \
      --no-editable

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12.10-slim AS runtime

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY services/execution-engine-service/src/ \
     ./services/execution-engine-service/src/
COPY services/execution-engine-service/alembic.ini \
     ./services/execution-engine-service/alembic.ini
COPY services/execution-engine-service/migrations/ \
     ./services/execution-engine-service/migrations/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/execution-engine-service/src"
WORKDIR /app/services/execution-engine-service

RUN addgroup --system svc && adduser --system --ingroup svc --no-create-home svc
RUN chown -R svc:svc /app
USER svc

EXPOSE 8000
CMD ["uvicorn", "oraclous_execution_engine_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
