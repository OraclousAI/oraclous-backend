"""Engine DTOs (ORAA-4 §21 schema layer) — Pydantic request/response models only.

organisation_id/user_id are never inbound (ORG001); both come from the authenticated principal in
the route. A job runs an OHM supplied inline (``manifest``) or by reference (``manifest_ref``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oraclous_execution_engine_service.models.enums import EngineJobState, ScheduleType


class HealthResponse(BaseModel):
    status: str
    service: str


class SubmitJobRequest(BaseModel):
    """Submit a durable harness job. Supply the OHM as exactly one of an inline parsed object
    (``manifest``) or a registered reference (``manifest_ref``). ``input`` is the goal."""

    manifest: dict[str, Any] | None = None
    manifest_ref: str | None = None
    input: str = Field(min_length=1)
    max_retries: int = Field(default=0, ge=0, le=10)
    timeout_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _exactly_one_manifest(self) -> SubmitJobRequest:
        if sum(x is not None for x in (self.manifest, self.manifest_ref)) != 1:
            raise ValueError("supply exactly one of 'manifest' or 'manifest_ref'")
        return self


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    user_id: uuid.UUID
    state: EngineJobState
    manifest_ref: str | None
    input_text: str
    harness_execution_id: uuid.UUID | None
    assignment_id: uuid.UUID | None
    schedule_id: uuid.UUID | None
    retry_count: int
    max_retries: int
    timeout_seconds: int | None
    progress: int
    output: str | None
    error_type: str | None
    error_message: str | None
    created_at: datetime | None
    updated_at: datetime | None


class JobListResponse(BaseModel):
    jobs: list[JobOut]
    total: int


class TaskListResponse(BaseModel):
    """The human task board — the org's ESCALATED jobs (each parked on a harness assignment)."""

    tasks: list[JobOut]
    total: int


class CompleteTaskRequest(BaseModel):
    """The human's output, forwarded to the harness; flips the parked run + the engine job."""

    output: str = Field(min_length=1)


class RegisterScheduleRequest(BaseModel):
    """Register a schedule that fires a harness job. A cron schedule needs a cron expression; the
    OHM is supplied inline (``manifest``) or by registry id (``manifest_ref``) — exactly one."""

    type: ScheduleType = ScheduleType.CRON
    cron: str | None = None
    manifest: dict[str, Any] | None = None
    manifest_ref: str | None = Field(default=None, max_length=512)
    input: str = Field(min_length=1)

    @model_validator(mode="after")
    def _exactly_one_manifest(self) -> RegisterScheduleRequest:
        if (self.manifest is None) == (self.manifest_ref is None):
            raise ValueError("supply exactly one of 'manifest' (inline) or 'manifest_ref'")
        return self


class ScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    type: str
    cron: str | None
    manifest_ref: str | None
    input_text: str
    enabled: bool
    last_fired_at: datetime | None
    created_at: datetime | None


class ScheduleListResponse(BaseModel):
    schedules: list[ScheduleOut]
    total: int
