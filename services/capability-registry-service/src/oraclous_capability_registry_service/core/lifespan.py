"""App lifecycle (ORAA-4 §21 core layer) — open/close the capability store.

The schema is created by the Alembic one-shot (not here). Degrades gracefully: if Postgres is
unreachable at startup the app still serves ``/health`` and the data routes report 503.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.repositories.instance_repository import InstanceRepository
from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    repo: CapabilityRepository | None = None
    instance_repo: InstanceRepository | None = None
    try:
        repo = CapabilityRepository(settings.DATABASE_URL)
        instance_repo = InstanceRepository(settings.DATABASE_URL)
        app.state.capability_repository = repo
        app.state.instance_repository = instance_repo
    except Exception as exc:  # noqa: BLE001 — degrade: data routes 503, /health still serves
        logger.warning("Postgres unavailable at startup; data routes disabled: %s", exc)
        app.state.capability_repository = None
        app.state.instance_repository = None

    # Seed the built-in tool catalogue into the dev org (idempotent plugin discovery). In jwt mode a
    # real per-org seed is driven elsewhere; the dev seam keeps the dev org's catalogue populated so
    # the stack is usable out of the box. A seed failure degrades to an empty catalogue (no crash).
    if repo is not None:
        try:
            statuses = await sync_plugins(
                repository=repo, organisation_id=uuid.UUID(settings.DEV_ORG_ID)
            )
            logger.info("seeded built-in tools into dev org: %s", statuses)
        except Exception as exc:  # noqa: BLE001 — degrade: catalogue empty, service still serves
            logger.warning("plugin seed skipped: %s", exc)

    try:
        yield
    finally:
        if repo is not None:
            await repo.close()
        if instance_repo is not None:
            await instance_repo.close()
