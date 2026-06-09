"""``WebhookSubscription`` storage model (R6 Slice 7 — the gateway webhook-ingress anchor).

Maps an opaque, unguessable inbound webhook id to {org, the published-agent it fires, the pinned
signature scheme, a REFERENCE to the broker-held signing secret}. The secret itself is NEVER here —
``broker_secret_ref`` is the cred-broker ``WebhookSecret`` id (ADR-008). Org-scoped (ADR-006): the
inbound POST resolves by ``id`` alone (the id is the bearer-less credential), and the org/target it
carries drive the engine fire — the external caller asserts nothing.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, String
from sqlalchemy.dialects.postgresql import UUID

from oraclous_application_gateway_service.models.base_model import BaseModel


class WebhookSubscription(BaseModel):
    __tablename__ = "webhook_subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # the opaque webhook id
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    target_slug = Column(String, nullable=False)  # a published-agent slug in this org
    signature_scheme = Column(String, nullable=False, default="generic")  # generic HMAC-SHA256 (v1)
    broker_secret_ref = Column(UUID(as_uuid=True), nullable=False)  # cred-broker WebhookSecret id
    enabled = Column(Boolean, nullable=False, default=True)  # fail-closed gate for the inbound POST
