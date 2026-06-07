"""Harness execution DTOs (ORAA-4 §21 schema layer) — Pydantic request/response models only.

``organisation_id`` is never inbound (ORG001) — it is resolved from the authenticated principal in
the route. The manifest is supplied inline (raw YAML or an already-parsed object); ``manifest_ref``
resolution against the registry lands in slice 2.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind


class HealthResponse(BaseModel):
    status: str
    service: str


class ExecuteHarnessRequest(BaseModel):
    """Run a harness. Supply the OHM exactly one of three ways: inline raw YAML (``manifest_yaml``),
    an already-parsed object (``manifest``), or by reference to a registered ``kind=harness``
    descriptor (``manifest_ref`` — a capability id). ``input`` is the goal handed to the agent."""

    manifest_yaml: str | None = None
    manifest: dict[str, Any] | None = None
    manifest_ref: str | None = None
    input: str = Field(min_length=1)

    @model_validator(mode="after")
    def _exactly_one_manifest(self) -> ExecuteHarnessRequest:
        provided = sum(
            x is not None for x in (self.manifest_yaml, self.manifest, self.manifest_ref)
        )
        if provided != 1:
            raise ValueError("supply exactly one of 'manifest_yaml', 'manifest', or 'manifest_ref'")
        return self


class StepOut(BaseModel):
    """One step in the tool-use loop's trace (an LLM turn or a capability dispatch)."""

    index: int
    kind: StepKind
    name: str  # the model role for an llm step; the capability binding for a tool step
    status: str  # "ok" | "error" (tool) / "answer" | "tool_calls" (llm)
    detail: str | None = None


class HarnessExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    harness_id: uuid.UUID
    harness_name: str
    content_hash: str | None
    status: HarnessStatus
    output: str | None
    error_type: str | None
    error_message: str | None
    iterations: int
    total_tokens: int
    steps: list[StepOut]
    created_at: datetime | None


class ExecutionListResponse(BaseModel):
    executions: list[HarnessExecutionOut]
    total: int


class AssignmentOut(BaseModel):
    """A human-actor task-board assignment (R4 creates it PENDING; resume is R5)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    execution_id: uuid.UUID
    harness_id: uuid.UUID
    human_role: str
    status: str
    input: str
    created_at: datetime | None


class AssignmentListResponse(BaseModel):
    assignments: list[AssignmentOut]
    total: int


class CompleteAssignmentRequest(BaseModel):
    """The human's output for a completed task — becomes the parked run's output on SUCCEEDED."""

    output: str = Field(min_length=1)


class ResumeHarnessRequest(BaseModel):
    """A human's decision on a mid-loop HITL pause. APPROVED resumes the loop (the gated tool runs);
    DENIED terminates the run FAILED. ``decision_reason`` is an optional audit note."""

    decision: Literal["APPROVED", "DENIED"]
    decision_reason: str | None = None
