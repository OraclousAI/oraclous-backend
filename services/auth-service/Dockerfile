# Build context: repo root (required for workspace uv.lock + pyproject.toml).
#
# Build:
#   docker build -f services/auth-service/Dockerfile -t oraclous-auth-service:dev .

FROM python:3.12.10-slim AS builder

# Install uv (pinned to match CI setup-uv version)
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

# Copy workspace config for lock-file resolution
COPY pyproject.toml uv.lock ./

# Copy shared packages (workspace members; needed for uv sync resolution)
COPY packages/ ./packages/

# Copy the service pyproject.toml
COPY services/auth-service/pyproject.toml ./services/auth-service/pyproject.toml

# Install the service's production dependencies into an isolated .venv
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync \
      --package oraclous-auth-service \
      --frozen \
      --no-dev \
      --no-editable

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12.10-slim AS runtime

WORKDIR /app

# Copy the populated virtualenv from the builder stage (packages installed non-editable)
COPY --from=builder /app/.venv /app/.venv

# Service source (packages are in the venv; only the service module is needed)
COPY services/auth-service/src/ ./services/auth-service/src/
# Migrations + alembic config (run by the auth-migrate one-shot)
COPY services/auth-service/migrations/ ./services/auth-service/migrations/
COPY services/auth-service/alembic.ini ./services/auth-service/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/auth-service/src"
WORKDIR /app/services/auth-service

RUN addgroup --system svc && adduser --system --ingroup svc --no-create-home svc
RUN chown -R svc:svc /app
USER svc

# Default: serve the API. The compose one-shot overrides this to run `alembic upgrade head`.
EXPOSE 8000
CMD ["uvicorn", "oraclous_auth_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
