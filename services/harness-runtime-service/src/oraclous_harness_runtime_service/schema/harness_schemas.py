"""Harness execution DTOs (ORAA-4 §21 schema layer) — Pydantic request/response models only.

``organisation_id`` is never inbound (ORG001) — it is resolved from the authenticated principal in
the route. The manifest is supplied inline (raw YAML or an already-parsed object); ``manifest_ref``
resolution against the registry lands in slice 2.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind


class HealthResponse(BaseModel):
    status: str
    service: str


class ExecuteHarnessRequest(BaseModel):
    """Run a harness. Supply the OHM inline as raw YAML (``manifest_yaml``) or as an already-parsed
    object (``manifest``) — exactly one. ``input`` is the goal/message handed to the entrypoint
    actor."""

    manifest_yaml: str | None = None
    manifest: dict[str, Any] | None = None
    input: str = Field(min_length=1)

    @model_validator(mode="after")
    def _exactly_one_manifest(self) -> ExecuteHarnessRequest:
        if (self.manifest_yaml is None) == (self.manifest is None):
            raise ValueError("supply exactly one of 'manifest_yaml' or 'manifest'")
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
    status: HarnessStatus
    output: str | None
    error_type: str | None
    error_message: str | None
    iterations: int
    steps: list[StepOut]
    created_at: datetime | None
