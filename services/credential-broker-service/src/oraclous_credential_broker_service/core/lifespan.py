"""App lifecycle (ORAA-4 §21 core layer) — open/close the credential + delegated-token stores.

The schema is created by the Alembic one-shot (not here). Degrades gracefully: if Postgres is
unreachable at startup the app still serves ``/health`` and the data routes report 503.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from oraclous_credential_broker_service.core.config import get_settings
from oraclous_credential_broker_service.repositories.credential_repository import (
    CredentialRepository,
)
from oraclous_credential_broker_service.repositories.postgres_delegated_token_store import (
    PostgresDelegatedTokenStore,
)
from oraclous_credential_broker_service.repositories.webhook_secret_repository import (
    WebhookSecretRepository,
)
from oraclous_credential_broker_service.services.delegation_service import DelegationService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    cred_repo: CredentialRepository | None = None
    webhook_repo: WebhookSecretRepository | None = None
    engine = None
    try:
        cred_repo = CredentialRepository(settings.DATABASE_URL)
        webhook_repo = WebhookSecretRepository(settings.DATABASE_URL)
        engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
        app.state.credential_repository = cred_repo
        app.state.webhook_secret_repository = webhook_repo
        app.state.delegation_service = DelegationService(
            store=PostgresDelegatedTokenStore(engine=engine)
        )
    except Exception as exc:  # noqa: BLE001 — degrade: data routes 503, /health still serves
        logger.warning("Postgres unavailable at startup; data routes disabled: %s", exc)
        app.state.credential_repository = None
        app.state.webhook_secret_repository = None
        app.state.delegation_service = None
    try:
        yield
    finally:
        if cred_repo is not None:
            await cred_repo.close()
        if webhook_repo is not None:
            await webhook_repo.close()
        if engine is not None:
            await engine.dispose()
