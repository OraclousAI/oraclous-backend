"""Per-org envelope encryption service (ORAA-4 §21 services layer, ADR-020).

The org-aware replacement for ``core/security``'s module-level encrypt/decrypt. ``encrypt`` always
writes the envelope (v2) under the org's DEK; ``decrypt`` is **format-polymorphic** — a v2
ciphertext goes through the DEK, anything else falls back to the legacy single-key v1 decrypt, so
the new code reads BOTH formats the moment it deploys (the zero-downtime cutover, ADR-020 §3).

The plaintext DEK is obtained by unwrapping the org's stored wrap via the KMS, cached in-process
for a short TTL (one AWS-KMS unwrap per org per window, not per secret). The cache holds only DEKs,
never the KEK. A DEK is created lazily on an org's first write (with a re-read on the UNIQUE-org
race).
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Callable
from typing import Any

from oraclous_credential_broker_service.core.envelope import (
    decrypt_with_dek,
    encrypt_with_dek,
    is_v2,
)
from oraclous_credential_broker_service.domain.kms import KmsProvider
from oraclous_credential_broker_service.repositories.org_data_key_repository import (
    OrgDataKeyRepository,
)


class EnvelopeService:
    def __init__(
        self,
        *,
        kms: KmsProvider,
        dek_repo: OrgDataKeyRepository,
        legacy_decrypt: Callable[[str], Any],
        dek_cache_ttl_seconds: int = 300,
    ) -> None:
        self._kms = kms
        self._deks = dek_repo
        self._legacy = legacy_decrypt
        self._ttl = dek_cache_ttl_seconds
        self._cache: dict[uuid.UUID, tuple[bytes, float]] = {}

    async def encrypt(self, *, organisation_id: uuid.UUID, plaintext: Any) -> str:
        """Encrypt under the org's DEK → a v2 envelope ciphertext (lazily creating the DEK)."""
        dek = await self._dek(organisation_id)
        return encrypt_with_dek(dek, organisation_id=str(organisation_id), plaintext=plaintext)

    async def decrypt(self, *, organisation_id: uuid.UUID, stored: str) -> Any:
        """Polymorphic: a v2 envelope via the org DEK, else the legacy single-key v1 path."""
        if is_v2(stored):
            dek = await self._dek(organisation_id)
            return decrypt_with_dek(dek, organisation_id=str(organisation_id), stored=stored)
        return self._legacy(stored)

    async def _dek(self, organisation_id: uuid.UUID) -> bytes:
        cached = self._cache.get(organisation_id)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        dek = await self._load_or_create_dek(organisation_id)
        self._cache[organisation_id] = (dek, time.monotonic() + self._ttl)
        return dek

    async def _load_or_create_dek(self, organisation_id: uuid.UUID) -> bytes:
        row = await self._deks.get_for_org(organisation_id=organisation_id)
        if row is None:
            # lazy first-write: mint a DEK, wrap it, persist the wrap. ``create`` returns the
            # AUTHORITATIVE row (the winner on a concurrent race), so we always unwrap the DEK
            # that actually persisted — never a discarded loser.
            _plaintext, wrapped = await self._kms.generate_data_key()
            row = await self._deks.create(
                organisation_id=organisation_id,
                wrapped_dek=base64.b64encode(wrapped).decode("ascii"),
                kek_provider=self._kms.key_id,
                kek_key_id=self._kms.key_id,
            )
        return await self._kms.decrypt_data_key(base64.b64decode(row.wrapped_dek))
