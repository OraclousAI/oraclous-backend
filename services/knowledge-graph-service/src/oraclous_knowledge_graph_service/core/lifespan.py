"""App lifecycle (ORAA-4 §21 core layer) — open/close shared connections.

The Postgres engine + sessionmaker and (when `KGS_NEO4J_URI` is set) the Neo4j driver are built once
at startup and disposed at shutdown, exposed on `app.state`. The Neo4j schema (org-scoped indexes)
is applied idempotently at startup. If Neo4j is unconfigured or unreachable, the API still serves
graph CRUD + /health; ingestion endpoints then return 503 (the driver is None). Connection setup is
the one driver concern allowed outside `repositories/` (§21 rule 3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from neo4j import Driver

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.database import make_engine, make_sessionmaker
from oraclous_knowledge_graph_service.core.neo4j import ensure_schema, make_neo4j_driver

logger = logging.getLogger(__name__)


def _open_neo4j() -> Driver | None:
    settings = get_settings()
    if not settings.neo4j_uri:
        return None
    driver = make_neo4j_driver(settings)
    ensure_schema(driver, database=settings.neo4j_database)
    return driver


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = make_engine()
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    driver: Driver | None = None
    try:
        driver = await asyncio.to_thread(_open_neo4j)
    except Exception as exc:  # noqa: BLE001 — degrade to CRUD-only, do not crash the app
        logger.warning("Neo4j unavailable at startup; ingestion disabled: %s", exc)
    app.state.neo4j_driver = driver

    try:
        yield
    finally:
        if driver is not None:
            await asyncio.to_thread(driver.close)
        await engine.dispose()
