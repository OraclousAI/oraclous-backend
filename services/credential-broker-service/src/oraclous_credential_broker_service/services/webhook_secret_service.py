"""Webhook-secret management (ORAA-4 §21 services layer) — mint + resolve, org-scoped.

``mint`` encrypts a signing secret at rest (AES-256-GCM); ``resolve`` returns the plaintext for the
trusted gateway to recompute an inbound HMAC over the raw body. Both are X-Internal-Key gated at the
route; a cross-org / missing / inactive id is a generic not-found (the route 404s — cross-org mask).
"""

from __future__ import annotations

import uuid

from oraclous_credential_broker_service.repositories.webhook_secret_repository import (
    WebhookSecretRepository,
)
from oraclous_credential_broker_service.services.envelope_service import EnvelopeService


class WebhookSecretNotFound(Exception):
    """No such (active) webhook secret in this org (-> 404, cross-org mask)."""


class WebhookSecretService:
    def __init__(self, repository: WebhookSecretRepository, *, envelope: EnvelopeService) -> None:
        self._repo = repository
        self._envelope = envelope

    async def mint(self, *, organisation_id: uuid.UUID, secret: str) -> uuid.UUID:
        encrypted = await self._envelope.encrypt(organisation_id=organisation_id, plaintext=secret)
        row = await self._repo.create(organisation_id=organisation_id, encrypted_secret=encrypted)
        return row.id

    async def resolve(self, *, secret_id: uuid.UUID, organisation_id: uuid.UUID) -> str:
        row = await self._repo.get_for_org(secret_id=secret_id, organisation_id=organisation_id)
        if row is None or row.status != "active":
            raise WebhookSecretNotFound(secret_id)
        return await self._envelope.decrypt(
            organisation_id=organisation_id, stored=row.encrypted_secret
        )

    async def delete(self, *, secret_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        """Hard-delete a secret (org-scoped, idempotent). Returns True if a row was removed. The
        gateway calls this to GC a webhook secret when its subscription is deleted / a create failed
        (R7-SEC S4) — gone-or-never-existed is a no-op (the caller treats both as success)."""
        return await self._repo.delete_for_org(secret_id=secret_id, organisation_id=organisation_id)
