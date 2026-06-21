"""Tool instance DTOs (schema layer) — Pydantic request/response models only.

``organisation_id`` and ``user_id`` are never inbound (ORG001): both are resolved from the
authenticated principal in the route. ``workflow_id`` is gone (workflows retired, ADR-005).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oraclous_capability_registry_service.models.enums import InstanceStatus


class CreateInstance(BaseModel):
    capability_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class ConfigureCredentials(BaseModel):
    """Map credential_type → credential_id (the credential lives in the credential-broker)."""

    credential_mappings: dict[str, str]


class InstanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    capability_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: str | None
    configuration: dict[str, Any]
    settings: dict[str, Any]
    credential_mappings: dict[str, str]
    required_credentials: list[str]
    status: InstanceStatus
    last_execution_id: uuid.UUID | None
    execution_count: int
    total_credits_consumed: Decimal
    created_at: datetime | None
    updated_at: datetime | None


class InstanceListResponse(BaseModel):
    instances: list[InstanceOut]
    total: int


class ValidationReport(BaseModel):
    is_ready: bool
    instance_id: uuid.UUID
    status: InstanceStatus
    checks: dict[str, str]
    errors: list[dict[str, Any]]
    action_items: list[dict[str, Any]]
