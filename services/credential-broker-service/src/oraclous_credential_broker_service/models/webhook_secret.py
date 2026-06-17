"""``WebhookSecret`` storage model (R6 Slice 7 — the gateway webhook-ingress signing secret).

A per-webhook HMAC signing secret, org-scoped, encrypted at rest with the same AES-256-GCM seam as
the user credentials (``core/security``). Unlike ``UserCredential`` it has NO owner/tool — a
webhook secret belongs to an org — so it is a separate table (mirroring the
``delegated_tokens`` precedent) rather than nullable-holing the personal-credential ORG001
invariants. The raw secret is recoverable (HMAC recomputes over the raw body), so it lives HERE —
in the broker, behind the X-Internal-Key seam — never in the stateless gateway (ADR-008).

ADR-006: ``organisation_id`` is the outermost tenancy scope (NOT NULL UUID).

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_credential_broker_service.models.base_model import BaseModel


class WebhookSecret(BaseModel):
    __tablename__ = "webhook_secrets"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )
    encrypted_secret: Mapped[str] = mapped_column(
        String, nullable=False
    )  # AES-256-GCM (core/security.encrypt_secret)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
