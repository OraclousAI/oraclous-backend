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
from oraclous_telemetry import Severity, alert, evaluate_readiness, exit_on_degrade_enabled

from oraclous_capability_registry_service.core.config import Settings, get_settings
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.repositories.execution_repository import (
    ExecutionRepository,
)
from oraclous_capability_registry_service.repositories.instance_repository import InstanceRepository
from oraclous_capability_registry_service.services.credential_client import (
    CredentialBrokerPort,
    FakeCredentialBroker,
    RealCredentialBroker,
    _libpq_dsn,
)
from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

logger = logging.getLogger(__name__)


def build_credential_broker(settings: Settings) -> CredentialBrokerPort:
    """Select the credential-broker implementation by ``CREDENTIAL_BROKER_MODE`` (fake default)."""
    if settings.CREDENTIAL_BROKER_MODE == "real":
        return RealCredentialBroker(
            base_url=settings.CREDENTIAL_BROKER_URL, internal_key=settings.INTERNAL_SERVICE_KEY
        )
    return FakeCredentialBroker(
        fake_db_dsn=settings.FAKE_DB_DSN or _libpq_dsn(settings.DATABASE_URL)
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    repo: CapabilityRepository | None = None
    instance_repo: InstanceRepository | None = None
    execution_repo: ExecutionRepository | None = None
    broker: CredentialBrokerPort | None = None
    try:
        repo = CapabilityRepository(
            settings.DATABASE_URL, platform_org_id=uuid.UUID(settings.PLATFORM_ORG_ID)
        )
        instance_repo = InstanceRepository(settings.DATABASE_URL)
        execution_repo = ExecutionRepository(settings.DATABASE_URL)
        broker = build_credential_broker(settings)
        app.state.capability_repository = repo
        app.state.instance_repository = instance_repo
        app.state.execution_repository = execution_repo
        app.state.credential_broker = broker
    except Exception as exc:  # noqa: BLE001 — degrade: data routes 503, /health reflects it
        app.state.capability_repository = None
        app.state.instance_repository = None
        app.state.execution_repository = None
        app.state.credential_broker = None
        alert(
            Severity.ERROR,
            "store_bind_failed",
            "capability-registry-service",
            "Postgres unavailable at startup; data routes disabled",
            store="postgres",
            error=str(exc),
        )

    verdict = evaluate_readiness({"postgres": app.state.capability_repository})
    if verdict.is_degraded and exit_on_degrade_enabled():
        raise SystemExit(1)

    # Seed the built-in tool catalogue into the platform org (idempotent plugin discovery). Every
    # tenant org reads it via the repository's widened reads, so a freshly-provisioned org sees the
    # global tools without per-org re-seeding. A seed failure degrades to an empty catalogue (no
    # crash).
    if repo is not None:
        try:
            statuses = await sync_plugins(
                repository=repo, organisation_id=uuid.UUID(settings.PLATFORM_ORG_ID)
            )
            logger.info("seeded built-in tools into platform org: %s", statuses)
        except Exception as exc:  # noqa: BLE001 — degrade: catalogue empty, service still serves
            logger.warning("plugin seed skipped: %s", exc)

    try:
        yield
    finally:
        if repo is not None:
            await repo.close()
        if instance_repo is not None:
            await instance_repo.close()
        if execution_repo is not None:
            await execution_repo.close()
        if broker is not None:
            await broker.aclose()
