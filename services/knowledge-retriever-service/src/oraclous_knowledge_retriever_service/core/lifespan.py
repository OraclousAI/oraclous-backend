"""App lifecycle (ORAA-4 §21 core layer) — open/close the read-only Neo4j driver (no schema)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from neo4j import Driver

from oraclous_knowledge_retriever_service.core.config import get_settings
from oraclous_knowledge_retriever_service.core.neo4j import make_neo4j_driver

logger = logging.getLogger(__name__)


def _open_neo4j() -> Driver | None:
    settings = get_settings()
    if not settings.neo4j_uri:
        return None
    # KRS is read-only (ORAA-58 / T6): it opens the driver but never creates schema/indexes.
    return make_neo4j_driver(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    driver: Driver | None = None
    try:
        driver = await asyncio.to_thread(_open_neo4j)
    except Exception as exc:  # noqa: BLE001 — degrade: retrieval routes 503, /health still serves
        logger.warning("Neo4j unavailable at startup; retrieval disabled: %s", exc)
    app.state.neo4j_driver = driver
    try:
        yield
    finally:
        if driver is not None:
            await asyncio.to_thread(driver.close)
