"""Execution DTOs (schema layer)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from oraclous_capability_registry_service.domain.run_context import PER_RUN_CONFIGURATION_KEYS
from oraclous_capability_registry_service.models.enums import ExecutionStatus


class ExecuteRequest(BaseModel):
    input_data: dict[str, Any] = Field(default_factory=dict)


class InternalExecuteRequest(ExecuteRequest):
    """A dispatch that also states WHICH RUN it belongs to (#1130).

    Only the ``/internal`` plane accepts this — stating a producer is asserting an identity, and on
    the member-facing plane any member of the organisation could have asserted a colleague's. The
    member-facing ``ExecuteRequest`` above has no such field, so a ``run_context`` posted there is
    ignored, not honoured.
    """

    run_context: dict[str, Any] | None = None

    @field_validator("run_context")
    @classmethod
    def _only_per_run_keys(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        unknown = sorted(set(value) - PER_RUN_CONFIGURATION_KEYS)
        if unknown:
            # The key NAMES are ours (they can only be names we refused), never the values.
            raise ValueError(f"run_context carries keys that are not per-run: {unknown}")
        return value


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
