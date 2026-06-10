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

from oraclous_credential_broker_service.core.config import Settings, get_settings
from oraclous_credential_broker_service.core.envelope import LocalKmsProvider, derive_local_kek
from oraclous_credential_broker_service.core.security import decrypt_secret
from oraclous_credential_broker_service.domain.kms import KmsProvider
from oraclous_credential_broker_service.repositories.aws_kms_provider import AwsKmsProvider
from oraclous_credential_broker_service.repositories.credential_repository import (
    CredentialRepository,
)
from oraclous_credential_broker_service.repositories.org_data_key_repository import (
    OrgDataKeyRepository,
)
from oraclous_credential_broker_service.repositories.postgres_delegated_token_store import (
    PostgresDelegatedTokenStore,
)
from oraclous_credential_broker_service.repositories.webhook_secret_repository import (
    WebhookSecretRepository,
)
from oraclous_credential_broker_service.services.delegation_service import DelegationService
from oraclous_credential_broker_service.services.envelope_service import EnvelopeService

logger = logging.getLogger(__name__)


def build_kms(settings: Settings) -> KmsProvider:
    """Select the KEK home (ADR-020). ``aws`` → a CMK in AWS KMS; ``local`` (default) → an explicit
    ``KMS_LOCAL_KEK``, or — when unset — a KEK HKDF-DERIVED from the legacy ENCRYPTION_KEY (existing
    deploys envelope without a new env var, while the KEK role stays cryptographically separate from
    the v1 data-key role)."""
    if settings.KMS_PROVIDER == "aws":
        return AwsKmsProvider(
            key_id=settings.KMS_AWS_KEY_ID, region=settings.KMS_AWS_REGION or None
        )
    kek = settings.KMS_LOCAL_KEK or derive_local_kek(settings.ENCRYPTION_KEY)
    return LocalKmsProvider(kek)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    cred_repo: CredentialRepository | None = None
    webhook_repo: WebhookSecretRepository | None = None
    dek_repo: OrgDataKeyRepository | None = None
    engine = None
    try:
        # the envelope is built FIRST (ADR-020): the credential repo encrypts THROUGH it (the
        # dependency-inverted `encrypt` callable), and the decrypt services read it off app.state.
        dek_repo = OrgDataKeyRepository(settings.DATABASE_URL)
        envelope = EnvelopeService(
            kms=build_kms(settings),
            dek_repo=dek_repo,
            legacy_decrypt=decrypt_secret,
            dek_cache_ttl_seconds=settings.KMS_DEK_CACHE_TTL_SECONDS,
        )
        cred_repo = CredentialRepository(settings.DATABASE_URL, encrypt=envelope.encrypt)
        webhook_repo = WebhookSecretRepository(settings.DATABASE_URL)
        engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
        app.state.envelope_service = envelope
        app.state.org_data_key_repository = dek_repo
        app.state.credential_repository = cred_repo
        app.state.webhook_secret_repository = webhook_repo
        app.state.delegation_service = DelegationService(
            store=PostgresDelegatedTokenStore(engine=engine)
        )
    except Exception as exc:  # noqa: BLE001 — degrade: data routes 503, /health still serves
        logger.warning("Postgres unavailable at startup; data routes disabled: %s", exc)
        app.state.envelope_service = None
        app.state.org_data_key_repository = None
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
        if dek_repo is not None:
            await dek_repo.close()
        if engine is not None:
            await engine.dispose()
