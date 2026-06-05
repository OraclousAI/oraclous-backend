"""App lifecycle (ORAA-4 §21 core layer) — open/close the capability store.

The schema is created by the Alembic one-shot (not here). Degrades gracefully: if Postgres is
unreachable at startup the app still serves ``/health`` and the data routes report 503.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    repo: CapabilityRepository | None = None
    try:
        repo = CapabilityRepository(settings.DATABASE_URL)
        app.state.capability_repository = repo
    except Exception as exc:  # noqa: BLE001 — degrade: data routes 503, /health still serves
        logger.warning("Postgres unavailable at startup; data routes disabled: %s", exc)
        app.state.capability_repository = None
    try:
        yield
    finally:
        if repo is not None:
            await repo.close()
