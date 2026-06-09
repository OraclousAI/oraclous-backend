"""Webhook-secret management (ORAA-4 §21 services layer) — mint + resolve, org-scoped.

``mint`` encrypts a signing secret at rest (AES-256-GCM); ``resolve`` returns the plaintext for the
trusted gateway to recompute an inbound HMAC over the raw body. Both are X-Internal-Key gated at the
route; a cross-org / missing / inactive id is a generic not-found (the route 404s — cross-org mask).
"""

from __future__ import annotations

import uuid

from oraclous_credential_broker_service.core.security import decrypt_secret, encrypt_secret
from oraclous_credential_broker_service.repositories.webhook_secret_repository import (
    WebhookSecretRepository,
)


class WebhookSecretNotFound(Exception):
    """No such (active) webhook secret in this org (-> 404, cross-org mask)."""


class WebhookSecretService:
    def __init__(self, repository: WebhookSecretRepository) -> None:
        self._repo = repository

    async def mint(self, *, organisation_id: uuid.UUID, secret: str) -> uuid.UUID:
        row = await self._repo.create(
            organisation_id=organisation_id, encrypted_secret=encrypt_secret(secret)
        )
        return row.id

    async def resolve(self, *, secret_id: uuid.UUID, organisation_id: uuid.UUID) -> str:
        row = await self._repo.get_for_org(secret_id=secret_id, organisation_id=organisation_id)
        if row is None or row.status != "active":
            raise WebhookSecretNotFound(secret_id)
        return decrypt_secret(row.encrypted_secret)
