"""CLI: run the ADR-020 envelope backfill (v1 → v2) against the configured DATABASE_URL.

    python -m oraclous_credential_broker_service.tasks.backfill_envelope

Idempotent + resumable — safe to re-run until it reports ``0`` rewrapped in every table. Run it
after deploying the envelope (writes already v2 + reads polymorphic); once every environment reports
zero v1 remaining, the legacy ``ENCRYPTION_KEY`` fallback can be retired (a separate signed-off op).
"""

from __future__ import annotations

import asyncio

from oraclous_credential_broker_service.core.config import get_settings
from oraclous_credential_broker_service.core.lifespan import build_kms
from oraclous_credential_broker_service.core.security import decrypt_secret
from oraclous_credential_broker_service.repositories.credential_repository import (
    CredentialRepository,
)
from oraclous_credential_broker_service.repositories.org_data_key_repository import (
    OrgDataKeyRepository,
)
from oraclous_credential_broker_service.repositories.webhook_secret_repository import (
    WebhookSecretRepository,
)
from oraclous_credential_broker_service.services.backfill_service import BackfillService
from oraclous_credential_broker_service.services.envelope_service import EnvelopeService


async def _run() -> dict[str, int]:
    settings = get_settings()
    dek_repo = OrgDataKeyRepository(settings.DATABASE_URL)
    envelope = EnvelopeService(
        kms=build_kms(settings),
        dek_repo=dek_repo,
        legacy_decrypt=decrypt_secret,
        dek_cache_ttl_seconds=settings.KMS_DEK_CACHE_TTL_SECONDS,
    )
    creds = CredentialRepository(settings.DATABASE_URL, encrypt=envelope.encrypt)
    whs = WebhookSecretRepository(settings.DATABASE_URL)
    try:
        return await BackfillService(
            envelope=envelope, credentials=creds, webhook_secrets=whs
        ).run()
    finally:
        await creds.close()
        await whs.close()
        await dek_repo.close()


def main() -> None:
    result = asyncio.run(_run())
    print(f"envelope backfill complete (v1->v2 rewrapped): {result}")  # noqa: T201 — CLI output


if __name__ == "__main__":
    main()
