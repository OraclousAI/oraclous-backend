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
    # an external capability ceiling (a team member's tools[] — ADR-032/035 §5): the runtime ceiling
    # is INTERSECTED with this, so a manifest_ref harness cannot exceed what the member declared.
    capability_ceiling: list[str] | None = None
    # run-tree correlation (ADR-037 Decision 3 / #471) — internal-plane only (engine→harness).
    # Correlation HINTS, never access grants: org stays the principal's (ORG001) and reads filter on
    # it, so a forged trace_id from org-A can't reach org-B's rows (H1/H4). trace_id None → the
    # harness mints it = this execution's id (the run-tree root).
    parent_execution_id: uuid.UUID | None = None
    trace_id: uuid.UUID | None = None

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
    # run-tree correlation (#471): trace_id groups the tree; parent is the dispatching execution.
    trace_id: uuid.UUID | None = None
    parent_execution_id: uuid.UUID | None = None


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


class ModelSpendOut(BaseModel):
    """Per-model spend estimate. ``estimated_usd`` is null + ``priced`` false for a model absent
    from the static rate table — it reports its raw tokens only, never a fabricated price."""

    model: str | None
    input_tokens: int
    output_tokens: int
    executions: int
    estimated_usd: float | None
    priced: bool


class SpendResponse(BaseModel):
    """An ESTIMATE of the user's provider LLM spend (BYOM), priced from a static rate table — NOT
    platform billing. Org-scoped. Unpriced models (absent from the table) report tokens only and are
    listed in ``unpriced_models``; ``total_estimated_usd`` sums only the priced rows."""

    since: datetime | None
    currency: str = "USD"
    by_model: list[ModelSpendOut]
    total_estimated_usd: float
    total_input_tokens: int
    total_output_tokens: int
    unpriced_models: list[str]


class ResumeHarnessRequest(BaseModel):
    """A human's decision on a mid-loop HITL pause. APPROVED resumes the loop (the gated tool runs);
    DENIED terminates the run FAILED. ``decision_reason`` is an optional audit note."""

    decision: Literal["APPROVED", "DENIED"]
    decision_reason: str | None = None
