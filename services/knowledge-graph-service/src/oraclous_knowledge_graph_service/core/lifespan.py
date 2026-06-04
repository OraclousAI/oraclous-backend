"""App lifecycle (ORAA-4 §21 core layer) — open/close shared connections.

The Postgres engine + sessionmaker are built once at startup and disposed at shutdown, then
exposed on `app.state` for the `get_sessionmaker` DI provider. Connection setup is the one
driver concern allowed outside `repositories/` (§21 rule 3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from oraclous_knowledge_graph_service.core.database import make_engine, make_sessionmaker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = make_engine()
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    try:
        yield
    finally:
        await engine.dispose()
