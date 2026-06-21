"""Uvicorn entrypoint — `app = create_app()`; nothing else."""

from __future__ import annotations

from oraclous_knowledge_retriever_service.app.factory import create_app

app = create_app()
