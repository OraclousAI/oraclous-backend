# Build context: repo root (required for workspace uv.lock + pyproject.toml).
#
# Build:
#   docker build -f services/capability-registry-service/Dockerfile \
#                -t oraclous-capability-registry-service:dev .
#
# Run:
#   docker run -p 8001:8000 \
#     -e DATABASE_URL=postgresql+asyncpg://oraclous:oraclous@host.docker.internal/oraclous \
#     -e INTERNAL_SERVICE_KEY=<key> \
#     oraclous-capability-registry-service:dev

FROM python:3.12.10-slim AS builder

# Install uv (pinned to match CI setup-uv version)
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

# Copy workspace config for lock-file resolution
COPY pyproject.toml uv.lock ./

# Copy shared packages (workspace members; needed for uv sync resolution)
COPY packages/ ./packages/

# Copy the service pyproject.toml
COPY services/capability-registry-service/pyproject.toml \
     ./services/capability-registry-service/pyproject.toml

# Install the service's production dependencies into an isolated .venv
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync \
      --package oraclous-capability-registry-service \
      --frozen \
      --no-dev \
      --no-editable

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12.10-slim AS runtime

WORKDIR /app

# Copy the populated virtualenv from the builder stage (packages installed non-editable)
COPY --from=builder /app/.venv /app/.venv

# Copy service source (packages are in the venv; only the service entrypoint is needed)
COPY services/capability-registry-service/src/ \
     ./services/capability-registry-service/src/
COPY services/capability-registry-service/migrations/ \
     ./services/capability-registry-service/migrations/
COPY services/capability-registry-service/alembic.ini \
     ./services/capability-registry-service/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/capability-registry-service/src"
WORKDIR /app/services/capability-registry-service

EXPOSE 8000

RUN addgroup --system svc && adduser --system --ingroup svc --no-create-home svc
RUN chown -R svc:svc /app
USER svc

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "oraclous_capability_registry_service.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]
