# Build context: repo root (required for workspace uv.lock + pyproject.toml).
#
# Build:
#   docker build -f services/knowledge-retriever-service/Dockerfile \
#                -t oraclous-knowledge-retriever-service:dev .
#
# Run:
#   docker run -p 8002:8000 oraclous-knowledge-retriever-service:dev

FROM python:3.12.10-slim AS builder

# Install uv (pinned to match CI setup-uv version)
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

# Copy workspace config for lock-file resolution
COPY pyproject.toml uv.lock ./

# Copy shared packages (workspace members; needed for uv sync resolution)
COPY packages/ ./packages/

# Copy the service pyproject.toml
COPY services/knowledge-retriever-service/pyproject.toml \
     ./services/knowledge-retriever-service/pyproject.toml

# Install the service's production dependencies into an isolated .venv
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync \
      --package oraclous-knowledge-retriever-service \
      --frozen \
      --no-dev \
      --no-editable

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12.10-slim AS runtime

WORKDIR /app

# Copy the populated virtualenv from the builder stage (packages installed non-editable)
COPY --from=builder /app/.venv /app/.venv

# Copy service source (packages are in the venv; only the service module is needed)
COPY services/knowledge-retriever-service/src/ \
     ./services/knowledge-retriever-service/src/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/knowledge-retriever-service/src"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", \
     "oraclous_knowledge_retriever_service.app.factory:create_app", \
     "--factory", \
     "--host", "0.0.0.0", \
     "--port", "8000"]
