"""Application package (ORAA-4 §21). `create_app` is the canonical assembly entrypoint."""

from __future__ import annotations

from oraclous_knowledge_retriever_service.app.factory import create_app

__all__ = ["create_app"]
