"""Envelope backfill (ORAA-4 §21 services layer, ADR-020 §3 step 3) — re-encrypt v1 → v2, online.

Walks every stored ciphertext (credentials + webhook secrets); a v2 value is skipped (idempotent), a
legacy v1 value is decrypted (polymorphic, via the single key) and re-encrypted under the org's DEK
(v2), then written back in place. Each row is independent + committed on write, so the sweep is
resumable. After it reports zero v1 remaining in every environment, the single ``ENCRYPTION_KEY``
fallback can be retired (a later, separately Reza-signed-off destructive step — NOT here).
"""

from __future__ import annotations

from oraclous_credential_broker_service.core.envelope import is_v2
from oraclous_credential_broker_service.repositories.credential_repository import (
    CredentialRepository,
)
from oraclous_credential_broker_service.repositories.webhook_secret_repository import (
    WebhookSecretRepository,
)
from oraclous_credential_broker_service.services.envelope_service import EnvelopeService


class BackfillService:
    def __init__(
        self,
        *,
        envelope: EnvelopeService,
        credentials: CredentialRepository,
        webhook_secrets: WebhookSecretRepository,
    ) -> None:
        self._env = envelope
        self._creds = credentials
        self._whs = webhook_secrets

    async def run(self) -> dict[str, int]:
        """Re-encrypt all v1 ciphertext to v2. Returns the per-table count of rows rewrapped."""
        return {
            "credentials": await self._backfill_credentials(),
            "webhook_secrets": await self._backfill_webhook_secrets(),
        }

    async def _backfill_credentials(self) -> int:
        rewrapped = 0
        for cid, org, stored in await self._creds.iter_all_ciphertexts():
            if is_v2(stored):
                continue
            plaintext = await self._env.decrypt(organisation_id=org, stored=stored)
            new_ct = await self._env.encrypt(organisation_id=org, plaintext=plaintext)
            await self._creds.set_encrypted_cred(cred_id=cid, encrypted_cred=new_ct)
            rewrapped += 1
        return rewrapped

    async def _backfill_webhook_secrets(self) -> int:
        rewrapped = 0
        for sid, org, stored in await self._whs.iter_all_ciphertexts():
            if is_v2(stored):
                continue
            plaintext = await self._env.decrypt(organisation_id=org, stored=stored)
            new_ct = await self._env.encrypt(organisation_id=org, plaintext=plaintext)
            await self._whs.set_encrypted_secret(secret_id=sid, encrypted_secret=new_ct)
            rewrapped += 1
        return rewrapped
