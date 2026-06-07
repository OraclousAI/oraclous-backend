"""Uvicorn entrypoint (ORAA-4 §21) — `app = create_app()`; nothing else."""

from __future__ import annotations

from oraclous_execution_engine_service.app.factory import create_app

app = create_app()
