"""Webhook subscription shapes (ORAA-4 §21 schema layer)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from oraclous_application_gateway_service.schema.published_agent_schemas import SLUG_PATTERN


class CreateSubscriptionRequest(BaseModel):
    agent_slug: str = Field(pattern=SLUG_PATTERN)  # a published agent in the member's org


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    target_slug: str
    signature_scheme: str
    enabled: bool
    created_at: datetime | None = None


class CreateSubscriptionResponse(BaseModel):
    """The created subscription PLUS the signing secret + the ingress path — both shown ONCE."""

    id: uuid.UUID
    agent_slug: str
    signature_scheme: str
    webhook_path: str  # POST here from the external source, signed with the secret below
    signing_secret: str  # the HMAC key; configure it on the source — it is never retrievable again
