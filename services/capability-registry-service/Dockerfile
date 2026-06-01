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

# Copy the populated virtualenv from the builder stage
COPY --from=builder /app/.venv /app/.venv

# Copy service source (installed as editable would need the source tree)
COPY services/capability-registry-service/src/ \
     ./services/capability-registry-service/src/

# Copy shared packages source (needed for any workspace package imports)
COPY packages/ ./packages/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/capability-registry-service/src:/app/packages/ohm/src:/app/packages/substrate/src:/app/packages/governance/src:/app/packages/provenance/src:/app/packages/rebac/src:/app/packages/telemetry/src:/app/packages/errors/src"

EXPOSE 8000

CMD ["uvicorn", "oraclous_capability_registry_service.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]
