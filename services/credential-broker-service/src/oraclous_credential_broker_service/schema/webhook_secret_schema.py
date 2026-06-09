"""Webhook-secret internal shapes (ORAA-4 §21 schema layer) — X-Internal-Key service-to-service.

``*Input`` (not ``*Request``): the trusted caller supplies ``organisation_id`` — the X-Internal-Key
gate is the control, so org-in-body is service→service plumbing (the ResolveCredentialInput idiom).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class WebhookSecretMintInput(BaseModel):
    organisation_id: uuid.UUID
    secret: str = Field(min_length=1)  # the raw HMAC signing secret, encrypted at rest


class WebhookSecretMintResponse(BaseModel):
    secret_id: uuid.UUID


class WebhookSecretResolveInput(BaseModel):
    organisation_id: uuid.UUID
    secret_id: uuid.UUID


class WebhookSecretResolveResponse(BaseModel):
    secret: str  # the decrypted signing secret, for the trusted gateway to recompute the HMAC
