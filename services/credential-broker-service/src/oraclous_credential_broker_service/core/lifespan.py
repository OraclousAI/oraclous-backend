"""App lifecycle (ORAA-4 §21 core layer) — open/close the credential + delegated-token stores.

The schema is created by the Alembic one-shot (not here). Degrades gracefully: if Postgres is
unreachable at startup the app still serves ``/health`` and the data routes report 503.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from oraclous_telemetry import Severity, alert, evaluate_readiness, exit_on_degrade_enabled

from oraclous_credential_broker_service.core.config import Settings, get_settings
from oraclous_credential_broker_service.core.envelope import LocalKmsProvider, derive_local_kek
from oraclous_credential_broker_service.core.rls import (
    RlsBypassingRoleError,
    assert_runtime_role_isolates,
    build_rls_engine,
)
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
        # ADR-030: the delegated-token engine carries the RLS org-GUC guard too.
        engine = build_rls_engine(settings.DATABASE_URL, pool_pre_ping=True)
        app.state.envelope_service = envelope
        app.state.org_data_key_repository = dek_repo
        app.state.credential_repository = cred_repo
        app.state.webhook_secret_repository = webhook_repo
        app.state.delegation_service = DelegationService(
            store=PostgresDelegatedTokenStore(engine=engine)
        )
    except Exception as exc:  # noqa: BLE001 — degrade: data routes 503, /health reflects it
        app.state.envelope_service = None
        app.state.org_data_key_repository = None
        app.state.credential_repository = None
        app.state.webhook_secret_repository = None
        app.state.delegation_service = None
        alert(
            Severity.ERROR,
            "store_bind_failed",
            "credential-broker-service",
            "Postgres unavailable at startup; data routes disabled",
            store="postgres",
            error=str(exc),
        )

    # ADR-030 §3: fail closed LOUDLY if the runtime role bypasses RLS (a superuser / BYPASSRLS role
    # makes the policy inert — T1-M3). Distinct from the Postgres-unavailable degrade above: a
    # mis-deployed bypassing role is a hard configuration error, so it exits the process rather than
    # quietly serving 503s. Gated on RLS_ASSERT_RUNTIME_ROLE (the deployed oraclous_app runtime sets
    # it; a deliberate owner-DSN dev/test run leaves it off). Only meaningful once the store bound.
    if settings.RLS_ASSERT_RUNTIME_ROLE and engine is not None:
        try:
            await assert_runtime_role_isolates(engine)
        except RlsBypassingRoleError as exc:
            alert(
                Severity.ERROR,
                "rls_runtime_role_bypasses",
                "credential-broker-service",
                "runtime DB role bypasses RLS; refusing to start (ADR-030 §3)",
                error=str(exc),
            )
            raise SystemExit(1) from exc

    verdict = evaluate_readiness({"postgres": app.state.credential_repository})
    if verdict.is_degraded and exit_on_degrade_enabled():
        raise SystemExit(1)

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
