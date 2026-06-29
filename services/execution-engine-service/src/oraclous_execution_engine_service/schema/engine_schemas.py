"""Engine DTOs (schema layer) — Pydantic request/response models only.

organisation_id/user_id are never inbound (ORG001); both come from the authenticated principal in
the route. A job runs an OHM supplied inline (``manifest``) or by reference (``manifest_ref``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oraclous_execution_engine_service.models.enums import (
    EngineJobState,
    ScheduleType,
    TargetKind,
)


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
    """Register a schedule. A cron schedule needs a cron expression. WHAT it fires is
    ``target_kind`` (#489): a ``harness_job`` (the default) supplies the OHM inline (``manifest``)
    or by registry id (``manifest_ref``) — exactly one — and no ``instance_id``; an
    ``adopted_tool_run`` supplies an ``instance_id`` (the curated/adopted capability-registry
    instance) + optional ``input_data``, and NO manifest."""

    type: ScheduleType = ScheduleType.CRON
    cron: str | None = None
    target_kind: TargetKind = TargetKind.HARNESS_JOB
    manifest: dict[str, Any] | None = None
    manifest_ref: str | None = Field(default=None, max_length=512)
    instance_id: uuid.UUID | None = None
    input_data: dict[str, Any] | None = None
    input: str = Field(min_length=1)

    @model_validator(mode="after")
    def _target_kind_shape(self) -> RegisterScheduleRequest:
        # The exactly-one-manifest rule is CONDITIONAL on target_kind (#489): harness_job keeps it;
        # adopted_tool_run forbids both manifests and requires an instance_id.
        if self.target_kind == TargetKind.HARNESS_JOB:
            if (self.manifest is None) == (self.manifest_ref is None):
                raise ValueError("supply exactly one of 'manifest' (inline) or 'manifest_ref'")
            if self.instance_id is not None:
                raise ValueError("'instance_id' is only for target_kind 'adopted_tool_run'")
        else:  # adopted_tool_run
            if self.manifest is not None or self.manifest_ref is not None:
                raise ValueError("an 'adopted_tool_run' schedule takes no manifest/manifest_ref")
            if self.instance_id is None:
                raise ValueError("an 'adopted_tool_run' schedule requires 'instance_id'")
        return self


class ScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    type: str
    cron: str | None
    target_kind: str
    manifest_ref: str | None
    instance_id: uuid.UUID | None
    input_text: str
    enabled: bool
    last_fired_at: datetime | None
    created_at: datetime | None


class ScheduleListResponse(BaseModel):
    schedules: list[ScheduleOut]
    total: int


class AdoptedToolRunOut(BaseModel):
    """One adopted-tool-run idempotency row a schedule produced (#489). ``execution_id`` is the
    registry ExecutionOut.id the worker dispatched + stamped (null until the worker stamps it)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    schedule_id: uuid.UUID
    idempotency_key: str
    execution_id: uuid.UUID | None
    created_at: datetime | None


class AdoptedToolRunListResponse(BaseModel):
    runs: list[AdoptedToolRunOut]
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
    # file-native blackboard (#518): the team's real working tree, threaded to every member's file
    # tools so they read/write it in place. Trusted per-run input — validated org-scoped at create
    # (must resolve under WORKSPACES_ROOT/<org>); None → the default per-org scratch sandbox.
    workspace_root: str | None = None
    # graph substrate (#524, ADR-040 Decision 7): the team's bound graph, threaded to every member's
    # graph tools (knowledge-retriever / graph-ingest / find-similar) so they target it. Trusted
    # per-run input — validated org-scoped at create (must belong to the caller's org via KGS);
    # None → the model supplies a graph_id per call / the KGS org-default graph.
    graph_id: str | None = None


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
    # the flow-evaluation verdict (#477) — PRODUCED + STORED at the gate, surfaced read-side here;
    # the run state is never branched on it (consuming it is E8). NULL until graded.
    verdict: dict[str, Any] | None = None
    # ADR-042 (#551): per-member terminal status — role -> "succeeded"|"failed"|"blocked"|"skipped".
    # A run is SUCCEEDED only when EVERY member is "succeeded"; on a FAILED run this is how a caller
    # sees which members to re-run (POST .../rerun re-drives the failed+blocked, keeping succeeded).
    member_status: dict[str, str] = Field(default_factory=dict)
    # ADR-043 (#552 PR-C + #553): per-loop conductor checkpoint, "<loop_index>" -> {round,
    # started_at, status, recalibration_count}. Surfaced read-side so an operator (and the e2e) can
    # see a loop's conductor activity — how many rounds it ran + how many bounded recalibrations it
    # spent. Additive + diagnostic; the run state never branches on it. Empty for an acyclic team.
    loop_state: dict[str, Any] = Field(default_factory=dict)
    # #585 (ADR-031 §D3): True when the run halted PARTIALLY on the team-pooled budget ceiling —
    # derived from the governed COST_BUDGET terminal (a budget halt is always partial), so a caller
    # (and the deployed e2e) can branch on the flag without string-matching the state.
    partial: bool = False

    @field_validator("member_status", "loop_state", mode="before")
    @classmethod
    def _coerce_member_status(cls, v: Any) -> Any:
        # A real flushed row already holds {} (the column default + migration 0012 server_default),
        # so a QUEUED run is {} not None. This only coerces None from a Python-constructed/unflushed
        # row (e.g. the route unit tests) or a hypothetical pre-migration NULL — fail-soft.
        return v or {}

    @model_validator(mode="after")
    def _derive_partial(self) -> TeamRunOut:
        if self.state == "COST_BUDGET":  # #585: a pooled-budget halt is always a partial run
            self.partial = True
        return self


class TeamRunTreeOut(BaseModel):
    """The run-tree of a team run (ADR-037 Decision 3 / #471): the root (= the trace_id threaded to
    every member) + the member harness execution ids the engine dispatched. Reassembled from the
    engine's OWN record — no cross-DB read into the harness. Org-scoped: a cross-org id is a 404."""

    team_run_id: uuid.UUID
    organisation_id: uuid.UUID
    root_execution_id: uuid.UUID | None
    state: str
    child_execution_ids: list[uuid.UUID]


class TeamRunCost(BaseModel):
    """A team run's metered cost. ``tokens`` are RAW counts (ADR-009 — the canonical metering, never
    a price). ``usd`` is a read-time price ESTIMATE, ``None`` until the per-model breakdown is wired
    (the harness holds the per-execution model; usd-by-trace is the documented follow-on)."""

    tokens: int
    usd: float | None = None


class TeamRunStatusOut(BaseModel):
    """O4 light status surface (ADR-037 Decision 5 / #472) — a one-glance 'is my team healthy / did
    it run / what did it cost' view for a standing team. ``progress`` is goal-attainment (member
    completion of the run-tree, 0–100), NOT the old hardcoded 5/100. Request-path org-scoped (H3): a
    cross-org id is a 404. No full-trace machinery — that is the opt-in run-tree (Decision 3)."""

    team_run_id: uuid.UUID
    organisation_id: uuid.UUID
    healthy: bool
    state: str
    progress: int
    last_run_at: datetime | None
    last_outcome: str
    cost: TeamRunCost
