"""Uvicorn entrypoint (ORAA-4 §21) — `app = create_app()`; nothing else.

Run with `uvicorn oraclous_knowledge_graph_service.main:app` or the `--factory` form against
`oraclous_knowledge_graph_service.app.factory:create_app`.
"""

from __future__ import annotations

from oraclous_knowledge_graph_service.app.factory import create_app

app = create_app()
