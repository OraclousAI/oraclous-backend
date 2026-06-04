# Build context: repo root (required for workspace uv.lock + pyproject.toml).
#
# Build:
#   docker build -f services/knowledge-retriever-service/Dockerfile \
#                -t oraclous-knowledge-retriever-service:dev .

FROM python:3.12.10-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY services/knowledge-retriever-service/pyproject.toml \
     ./services/knowledge-retriever-service/pyproject.toml

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync \
      --package oraclous-knowledge-retriever-service \
      --frozen \
      --no-dev \
      --no-editable

# ── Runtime stage ────────────────────────────────────────────────────────────

FROM python:3.12.10-slim AS runtime

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY services/knowledge-retriever-service/src/ \
     ./services/knowledge-retriever-service/src/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/knowledge-retriever-service/src"
WORKDIR /app/services/knowledge-retriever-service

RUN addgroup --system svc && adduser --system --ingroup svc --no-create-home svc
RUN chown -R svc:svc /app
USER svc

EXPOSE 8000
CMD ["uvicorn", "oraclous_knowledge_retriever_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
