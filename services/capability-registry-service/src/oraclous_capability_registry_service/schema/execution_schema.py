"""Execution DTOs (ORAA-4 §21 schema layer)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oraclous_capability_registry_service.models.enums import ExecutionStatus


class ExecuteRequest(BaseModel):
    input_data: dict[str, Any] = Field(default_factory=dict)


class ExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    instance_id: uuid.UUID
    capability_id: uuid.UUID
    user_id: uuid.UUID
    status: ExecutionStatus
    output_data: dict[str, Any] | None
    credential_refs: list[dict[str, Any]] | None
    error_message: str | None
    error_type: str | None
    credits_consumed: Decimal
    processing_time_ms: int | None
    created_at: datetime | None
