"""``WebhookSubscription`` storage model (R6 Slice 7 — the gateway webhook-ingress anchor).

Maps an opaque, unguessable inbound webhook id to {org, the published-agent it fires, the pinned
signature scheme, a REFERENCE to the broker-held signing secret}. The secret itself is NEVER here —
``broker_secret_ref`` is the cred-broker ``WebhookSecret`` id (ADR-008). Org-scoped (ADR-006): the
inbound POST resolves by ``id`` alone (the id is the bearer-less credential), and the org/target it
carries drive the engine fire — the external caller asserts nothing.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_application_gateway_service.models.base_model import BaseModel


class WebhookSubscription(BaseModel):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )  # the opaque webhook id
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    target_slug: Mapped[str] = mapped_column(
        String, nullable=False
    )  # a published-agent slug in this org
    signature_scheme: Mapped[str] = mapped_column(
        String, nullable=False, default="generic"
    )  # generic HMAC-SHA256 (v1)
    broker_secret_ref: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )  # cred-broker WebhookSecret id
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )  # fail-closed gate for the inbound POST
