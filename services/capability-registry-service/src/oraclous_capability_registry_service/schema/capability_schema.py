"""Capability registry DTOs (ORAA-4 §21 schema layer) — Pydantic request/response models only.

``organisation_id`` is never an inbound field (ORG001): it is resolved from the authenticated
principal in the route. It appears only on the *response* projection.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oraclous_capability_registry_service.models.enums import DescriptorKind


class CreateCapability(BaseModel):
    """Register a capability descriptor. ``descriptor`` is the OHM manifest JSONB."""

    kind: DescriptorKind = DescriptorKind.TOOL
    descriptor: dict[str, Any]
    # Optional deterministic id (e.g. a tool's UUIDv5); omitted → server-generated uuid4.
    descriptor_id: uuid.UUID | None = None


class RegisterTool(BaseModel):
    """Register a tool. ``kind`` is implicitly ``tool``; the id is derived deterministically from
    the descriptor's ``metadata.name``/``version``/``category`` (no caller-supplied id)."""

    descriptor: dict[str, Any]


class UpdateCapability(BaseModel):
    descriptor: dict[str, Any]


class MatchCapabilitiesRequest(BaseModel):
    capabilities: list[str] = Field(default_factory=list)


class CapabilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    kind: DescriptorKind
    name: str | None
    content_hash: str | None
    descriptor: dict[str, Any]
    created_at: datetime | None
    updated_at: datetime | None


class CapabilityListResponse(BaseModel):
    capabilities: list[CapabilityOut]
    total: int
