# Build context: repo root (required for workspace uv.lock + pyproject.toml).
#
# Build:
#   docker build -f services/knowledge-graph-service/Dockerfile \
#                -t oraclous-knowledge-graph-service:dev .
#
# Run:
#   docker run --rm oraclous-knowledge-graph-service:dev

FROM python:3.12.10-slim AS builder

# Install uv (pinned to match CI setup-uv version)
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

# Copy workspace config for lock-file resolution
COPY pyproject.toml uv.lock ./

# Copy shared packages (workspace members; needed for uv sync resolution)
COPY packages/ ./packages/

# Copy the service pyproject.toml
COPY services/knowledge-graph-service/pyproject.toml \
     ./services/knowledge-graph-service/pyproject.toml

# Install the service's production dependencies into an isolated .venv
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync \
      --package oraclous-knowledge-graph-service \
      --frozen \
      --no-dev \
      --no-editable

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12.10-slim AS runtime

WORKDIR /app

# Copy the populated virtualenv from the builder stage (packages installed non-editable)
COPY --from=builder /app/.venv /app/.venv

# Copy service source (packages are in the venv; only the service module is needed)
COPY services/knowledge-graph-service/src/ \
     ./services/knowledge-graph-service/src/
# Migrations + alembic config + dev seed (run by the kgs-migrate / kgs-seed one-shots)
COPY services/knowledge-graph-service/migrations/ \
     ./services/knowledge-graph-service/migrations/
COPY services/knowledge-graph-service/alembic.ini \
     ./services/knowledge-graph-service/alembic.ini
COPY services/knowledge-graph-service/scripts/ \
     ./services/knowledge-graph-service/scripts/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/knowledge-graph-service/src"
WORKDIR /app/services/knowledge-graph-service

RUN addgroup --system svc && adduser --system --ingroup svc --no-create-home svc
RUN chown -R svc:svc /app
USER svc

# Default: serve the API. The compose one-shots override this to run
# `alembic upgrade head` (kgs-migrate) and `python scripts/seed_dev.py` (kgs-seed).
EXPOSE 8000
CMD ["uvicorn", "oraclous_knowledge_graph_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
