"""Engine DTOs (ORAA-4 §21 schema layer) — Pydantic request/response models only.

organisation_id/user_id are never inbound (ORG001); both come from the authenticated principal in
the route. A job runs an OHM supplied inline (``manifest``) or by reference (``manifest_ref``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

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


class EngineEventRequest(BaseModel):
    """Fire a webhook EVENT (gateway-attested) -> a durable job. The gateway supplies the resolved
    OHM (exactly one of inline ``manifest`` or ``manifest_ref``), the ``input`` from the event, and
    a dedupe ``idempotency_key`` (the provider delivery id). ``event_type``/``source`` are audit
    only. The org is taken from the gateway-asserted principal, NEVER this body (ADR-006)."""

    manifest: dict[str, Any] | None = None
    manifest_ref: str | None = None
    input: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=255)
    event_type: str | None = None
    source: str | None = None

    @model_validator(mode="after")
    def _exactly_one_manifest(self) -> EngineEventRequest:
        if sum(x is not None for x in (self.manifest, self.manifest_ref)) != 1:
            raise ValueError("supply exactly one of 'manifest' or 'manifest_ref'")
        return self


class EngineEventResponse(BaseModel):
    """A webhook event-fire outcome — always 202. ``deduped`` marks a re-delivered event (no new
    job created); ``job_id`` is the durable job for a fresh delivery."""

    accepted: bool = True
    deduped: bool
    job_id: uuid.UUID | None = None


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


class ApproveTaskRequest(BaseModel):
    """A human's decision on a mid-loop HITL approval task. APPROVED resumes the harness loop (the
    gated tool runs); DENIED terminates the run FAILED. ``decision_reason`` is an optional note."""

    decision: Literal["APPROVED", "DENIED"]
    decision_reason: str | None = None


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


class ActivityEvent(BaseModel):
    """One provenance/audit event in the org's activity feed (read-only projection of
    ``engine_provenance``). Org-scoped to the caller — never another tenant's row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: str
    resource: str
    outcome: str
    created_at: datetime | None


class ActivityResponse(BaseModel):
    """The org's most-recent provenance events, newest-first (capped by ``limit``)."""

    events: list[ActivityEvent]
    total: int


class UsageCount(BaseModel):
    """A RAW usage signal: how many provenance events the org recorded for one ``action``. Per
    ADR-009 this is a COUNT only — never a price, USD, or credits (those are downstream rates)."""

    action: str
    count: int


class UsageResponse(BaseModel):
    """The org's RAW per-action usage counts (ADR-009 — counts, not money). ``since`` echoes the
    requested window lower-bound (null = all-time); ``total_events`` is the sum across actions."""

    usage: list[UsageCount]
    total_events: int
    since: datetime | None = None


class RoundtableActorIn(BaseModel):
    """One participant. An ``agent`` actor runs an OHM (inline ``manifest`` or ``manifest_ref``); a
    ``human`` actor pauses the round-table to respond. ``role`` labels its turns in the script."""

    role: str = Field(min_length=1, max_length=128)
    kind: Literal["agent", "human"]
    manifest: dict[str, Any] | None = None
    manifest_ref: str | None = Field(default=None, max_length=512)
    prompt: str | None = None

    @model_validator(mode="after")
    def _agent_needs_a_manifest(self) -> RoundtableActorIn:
        if self.kind == "agent" and not (self.manifest or self.manifest_ref):
            raise ValueError("an agent actor needs a 'manifest' or 'manifest_ref'")
        return self


class CreateRoundtableRequest(BaseModel):
    """Start a round-table: a topic + ≥1 ordered actors, driven for ``max_rounds`` full rounds."""

    topic: str = Field(min_length=1)
    actors: list[RoundtableActorIn] = Field(min_length=1, max_length=16)
    max_rounds: int = Field(default=1, ge=1, le=10)


class RespondRoundtableRequest(BaseModel):
    """A human's contribution to the paused turn; appended to the transcript, the driver resumes."""

    output: str = Field(min_length=1)


class RoundtableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    topic: str
    state: str
    current_turn: int
    max_rounds: int
    transcript: list[dict[str, Any]]
    final_output: str | None
    error_message: str | None
    created_at: datetime | None


class CreateTeamRunRequest(BaseModel):
    """Run an OHM v1.1 Team Harness: the team manifest + the per-role generated sub-harnesses.

    ``manifest`` is the Team Harness OHM (``metadata.kind == "team"`` with ``members``).
    ``sub_harnesses`` maps each member ``role`` to its generated single-agent sub-harness OHM (run
    inline by the harness);
    a role without one falls back to the member's ``manifest_ref``. ``gate_decisions`` pre-seeds any
    human-gate decisions (role → ``approve`` | ``reject``) for a run that should not pause.
    """

    manifest: dict[str, Any]
    sub_harnesses: dict[str, dict[str, Any]] = Field(default_factory=dict)
    gate_decisions: dict[str, Literal["approve", "reject"]] = Field(default_factory=dict)


class AdvanceTeamRunRequest(BaseModel):
    """Advance a PAUSED team run past its human gate(s): role → ``approve`` | ``reject``."""

    gate_decisions: dict[str, Literal["approve", "reject"]] = Field(min_length=1)


class TeamRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    state: str
    results: dict[str, Any]
    paused_at: list[str]
    error_message: str | None
    created_at: datetime | None


class TeamRunTreeOut(BaseModel):
    """The run-tree of a team run (ADR-037 Decision 3 / #471): the root (= the trace_id threaded to
    every member) + the member harness execution ids the engine dispatched. Reassembled from the
    engine's OWN record — no cross-DB read into the harness. Org-scoped: a cross-org id is a 404."""

    team_run_id: uuid.UUID
    organisation_id: uuid.UUID
    root_execution_id: uuid.UUID | None
    state: str
    child_execution_ids: list[uuid.UUID]
