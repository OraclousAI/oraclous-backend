"""Engine DTOs (schema layer) — Pydantic request/response models only.

organisation_id/user_id are never inbound (ORG001); both come from the authenticated principal in
the route. A job runs an OHM supplied inline (``manifest``) or by reference (``manifest_ref``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from oraclous_ohm.gate import GateDecision
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oraclous_execution_engine_service.domain.schedule_cost import (
    DEFAULT_EXPECTED_INPUT_TOKENS,
    DEFAULT_EXPECTED_OUTPUT_TOKENS,
)
from oraclous_execution_engine_service.models.enums import (
    BudgetPeriod,
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
    manifest_ref: str | None = Field(default=None, max_length=512)
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
    manifest_ref: str | None = Field(default=None, max_length=512)
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
    # bound to the EngineSchedule.cron VARCHAR(128) — a valid-but-long cron (a comma-enumerated
    # minute field passes croniter.is_valid at >128 chars) would else overflow the column INSERT
    # (StringDataRightTruncation) → a 500 instead of a clean 422 (CTO review #622 sibling-sweep).
    cron: str | None = Field(default=None, max_length=128)
    target_kind: TargetKind = TargetKind.HARNESS_JOB
    manifest: dict[str, Any] | None = None
    manifest_ref: str | None = Field(default=None, max_length=512)
    instance_id: uuid.UUID | None = None
    input_data: dict[str, Any] | None = None
    # #601 (team only): the persistent graph workspace the standing team's runs read+write
    graph_id: str | None = Field(default=None, max_length=512)
    # #598 (team only): the L3 schedule-level recurring per-period cap. Both set => active; both
    # unset => OFF (the default). A period with no allowance (or vice versa) is a no-op/fail-open
    # cap, rejected below. Token-only this slice (USD defers to a pricing seam, ADR-009).
    budget_period: BudgetPeriod | None = None
    budget_allowance_tokens: int | None = Field(default=None, gt=0)
    input: str = Field(min_length=1)

    @model_validator(mode="after")
    def _target_kind_shape(self) -> RegisterScheduleRequest:
        # The exactly-one-manifest rule is CONDITIONAL on target_kind (#489): harness_job keeps it;
        # adopted_tool_run forbids both manifests + requires an instance_id; team (#601) requires an
        # inline manifest + a graph_id.
        if self.target_kind == TargetKind.HARNESS_JOB:
            if (self.manifest is None) == (self.manifest_ref is None):
                raise ValueError("supply exactly one of 'manifest' (inline) or 'manifest_ref'")
            if self.instance_id is not None:
                raise ValueError("'instance_id' is only for target_kind 'adopted_tool_run'")
        elif self.target_kind == TargetKind.ADOPTED_TOOL_RUN:
            if self.manifest is not None or self.manifest_ref is not None:
                raise ValueError("an 'adopted_tool_run' schedule takes no manifest/manifest_ref")
            if self.instance_id is None:
                raise ValueError("an 'adopted_tool_run' schedule requires 'instance_id'")
        else:  # team (#601): an inline team manifest + a persistent graph workspace
            if self.manifest is None:
                raise ValueError("a 'team' schedule requires an inline 'manifest'")
            if self.manifest_ref is not None:
                raise ValueError("a 'team' schedule takes 'manifest', not 'manifest_ref'")
            if self.instance_id is not None:
                raise ValueError("'instance_id' is only for target_kind 'adopted_tool_run'")
            if not self.graph_id:
                raise ValueError("a 'team' schedule requires a 'graph_id'")
        # #598: the L3 cap is team-only, recurring, and all-or-nothing (fail-closed). The service
        # re-validates (the authoritative gate); this surfaces a clean 422 at the edge.
        if (self.budget_period is None) != (self.budget_allowance_tokens is None):
            raise ValueError(
                "a per-period budget needs BOTH 'budget_period' and 'budget_allowance_tokens'"
            )
        if self.budget_period is not None and (
            self.target_kind != TargetKind.TEAM or self.type != ScheduleType.CRON
        ):
            raise ValueError("a per-period budget cap is only for a recurring (cron) team schedule")
        return self


class PreflightScheduleRequest(BaseModel):
    """#603 dec-4(a): a cadence-aware cost pre-flight — "~$X/day at this cadence" BEFORE GO. Prices
    an INLINE team manifest at a proposed ``cron`` without creating/enabling anything. Its
    ``input_data`` carries the per-member ``sub_harnesses`` (as a schedule's does) so each model
    binding is read the same way it will fire. Tokens default to a rough per-agent turn estimate."""

    manifest: dict[str, Any]
    cron: str = Field(min_length=1, max_length=128)
    input_data: dict[str, Any] | None = None
    expected_input_tokens: int = Field(default=DEFAULT_EXPECTED_INPUT_TOKENS, ge=1)
    expected_output_tokens: int = Field(default=DEFAULT_EXPECTED_OUTPUT_TOKENS, ge=1)


class MemberCostOut(BaseModel):
    """One member's projected recurring cost. ``priced`` false (+ null usd) when its binding is
    unset/unknown — reported unpriced, NEVER a fabricated $0."""

    role: str
    binding: str | None
    priced: bool
    usd_per_fire: float | None
    usd_per_day: float | None


class PreflightCostResponse(BaseModel):
    """The forward "~$X/day at this cadence" projection. ``fleet_usd_per_day`` sums ONLY priced
    members; ``unpriced_members`` names the rest so a partial total is never read as "free"."""

    currency: str = "USD"
    cadence_fires_per_day: float
    fleet_usd_per_day: float
    per_member: list[MemberCostOut]
    unpriced_members: list[str]


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
    # #601: the team schedule's bound graph workspace + the per-cadence cost accumulator (which #598
    # reinterprets as the CURRENT-WINDOW spend when a period cap is set).
    graph_id: str | None = None
    recurring_cost_tokens: int = 0
    # #598 (ADR-048 dec 4b O4 surface): the L3 per-period cap + its pause state, so a caller sees
    # the window spend, the ceiling, and WHY a team is disabled (budget_paused True = paused by the
    # cap, not a manual disable; resumes at the next window boundary).
    budget_period: str | None = None
    budget_allowance_tokens: int | None = None
    budget_window_start: datetime | None = None
    budget_paused: bool = False


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


class ScheduledTeamRunOut(BaseModel):
    """#601: one team-run a standing-team schedule produced — the readable proof a fire created a
    run BOUND to the schedule's persistent ``graph_id`` (the keystone), with its state + cost."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    schedule_id: uuid.UUID | None
    graph_id: str | None
    state: str
    cost_tokens: int
    created_at: datetime | None


class ScheduledTeamRunListResponse(BaseModel):
    runs: list[ScheduledTeamRunOut]
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
    human-gate decisions (role → a ``GateDecision`` — ``approve`` | ``revise`` | ``reject``, ADR-046
    / #578; a v1 bare ``approve``/``reject`` string still parses) for a run that should not pause.
    """

    manifest: dict[str, Any]
    sub_harnesses: dict[str, dict[str, Any]] = Field(default_factory=dict)
    gate_decisions: dict[str, GateDecision] = Field(default_factory=dict)
    # file-native blackboard (#518): the team's real working tree, threaded to every member's file
    # tools so they read/write it in place. Trusted per-run input — validated org-scoped at create
    # (must resolve under WORKSPACES_ROOT/<org>); None → the default per-org scratch sandbox.
    workspace_root: str | None = None
    # graph substrate (#524, ADR-040 Decision 7): the team's bound graph, threaded to every member's
    # graph tools (knowledge-retriever / graph-ingest / find-similar) so they target it. Trusted
    # per-run input — validated org-scoped at create (must belong to the caller's org via KGS);
    # None → the model supplies a graph_id per call / the KGS org-default graph.
    graph_id: str | None = None
    # #599: user-seeded team state — a fan_out.over: "$.<key>" resolves a provided list.
    inputs: dict[str, Any] | None = None
    # #602 (seeded-refresh, ADR-048 dec 3): the NAMED prior run whose stored output seeds this run.
    # Its records are threaded in as state so the producing member carries-forward unchanged ones,
    # and at settle the engine emits a first-class 5-way what-changed delta. Validated org-scoped +
    # SUCCEEDED-only at create (fail-fast 422); None → a normal (non-refresh) run.
    seed_from_run_id: uuid.UUID | None = None


class AdvanceTeamRunRequest(BaseModel):
    """Advance a PAUSED team run: role → a ``GateDecision`` (``approve`` | ``revise`` | ``reject``,
    ADR-046 / #578). ``revise`` re-runs the gate's producer sub-tree with ``feedback`` (or seeds
    ``edited_payload``) and re-pauses. A v1 bare ``"approve"``/``"reject"`` string still parses."""

    gate_decisions: dict[str, GateDecision] = Field(min_length=1)


class TeamRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    state: str
    results: dict[str, Any]
    paused_at: list[str]
    error_message: str | None
    created_at: datetime | None
    # the flow-evaluation verdict (#477) — PRODUCED + STORED at the gate, surfaced read-side here.
    # #604 (ADR-048 dec 5): at settle the run is now BRANCHED on it — a below-threshold verdict
    # re_task-re-dispatches or escalates (PAUSED) via the closed loop. NULL until graded.
    verdict: dict[str, Any] | None = None
    # #604: how many times the closed loop re-dispatched (re_task) this run — surfaced so a caller
    # (and the e2e) sees the run was re-dispatched, not blindly re-run; 0 on a normal/first settle.
    re_dispatch_count: int = 0
    # #602 (seeded-refresh, ADR-048 dec 3): the first-class 5-way what-changed delta — per-record
    # {added, removed, changed, unchanged, re_confirmed} + counts — computed at settle by comparing
    # this run's records to the seed run's. NULL on a non-refresh run; the refresh's contract.
    refresh_delta: dict[str, Any] | None = None
    # ADR-042 (#551): per-member terminal status — role -> "succeeded"|"failed"|"blocked"|"skipped"|
    # "budget_skipped" (#585, un-attempted by the pooled budget)|"partial" (#587, degraded).
    # A run is SUCCEEDED only when EVERY member is "succeeded"; on a FAILED run this is how a caller
    # sees which members to re-run (POST .../rerun re-drives the failed+blocked, keeping succeeded).
    member_status: dict[str, str] = Field(default_factory=dict)
    # ADR-046 (#578): role -> how many times this human gate was REVISED. Surfaced read-side so a
    # caller (and the deployed e2e) sees a run was revised + on which gate; the revision loop
    # fail-closes to terminal REJECTED once a gate exceeds max_revisions. Empty until a revise.
    revision_rounds: dict[str, int] = Field(default_factory=dict)
    # ADR-043 (#552 PR-C + #553): per-loop conductor checkpoint, "<loop_index>" -> {round,
    # started_at, status, recalibration_count}. Surfaced read-side so an operator (and the e2e) can
    # see a loop's conductor activity — how many rounds it ran + how many bounded recalibrations it
    # spent. Additive + diagnostic; the run state never branches on it. Empty for an acyclic team.
    loop_state: dict[str, Any] = Field(default_factory=dict)
    # #585 (ADR-031 §D3): True when the run halted PARTIALLY on the team-pooled budget ceiling —
    # derived from the governed COST_BUDGET terminal (a budget halt is always partial), so a caller
    # (and the deployed e2e) can branch on the flag without string-matching the state.
    partial: bool = False
    # #638: read-side context the console needs (oraclous-frontend#180). ``graph_id`` is the run's
    # bound graph (the Results tab reads artifacts from that workspace) — a direct column, so
    # from_attributes fills it. ``team_name`` is dug from the stored manifest.metadata.name below
    # (like the #634 list row) — additive, non-breaking.
    graph_id: str | None = None
    team_name: str | None = None
    # the source for ``team_name`` (metadata.name is not itself a TeamRunOut field). Loaded from the
    # row via from_attributes, read by the validator below, then EXCLUDED from the response — the
    # detail read never carries the raw manifest (leanness + no accidental leak).
    manifest: dict[str, Any] | None = Field(default=None, exclude=True, repr=False)

    @field_validator("member_status", "loop_state", "revision_rounds", mode="before")
    @classmethod
    def _coerce_member_status(cls, v: Any) -> Any:
        # A real flushed row already holds {} (the column default + migration 0012 server_default),
        # so a QUEUED run is {} not None. This only coerces None from a Python-constructed/unflushed
        # row (e.g. the route unit tests) or a hypothetical pre-migration NULL — fail-soft.
        return v or {}

    @field_validator("re_dispatch_count", mode="before")
    @classmethod
    def _coerce_re_dispatch_count(cls, v: Any) -> Any:
        # A real flushed row holds 0 (the migration 0018 server_default '0'); this coerces None from
        # a Python-constructed/unflushed row (e.g. the route unit tests) — else int-validation 500s.
        return v or 0

    @model_validator(mode="after")
    def _derive_partial(self) -> TeamRunOut:
        if self.state == "COST_BUDGET":  # #585: a pooled-budget halt is always a partial run
            self.partial = True
        # #638: dig the team name from the stored manifest (metadata.name) so every read carries it.
        if self.team_name is None and isinstance(self.manifest, dict):
            self.team_name = ((self.manifest.get("metadata") or {}).get("name")) or None
        return self


class TeamRunStateFilter(StrEnum):
    """#633: the team-run states a LIST query may filter on (repeatable; an unknown value is a 422,
    enforced natively by FastAPI at the route). Mirrors the EngineTeamRun state machine — the
    transient states, terminal SUCCEEDED/FAILED, the #578 REJECTED gate terminal, and the #585
    COST_BUDGET governed pooled-budget halt."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PAUSED = "PAUSED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    COST_BUDGET = "COST_BUDGET"


class TeamRunListItem(BaseModel):
    """#633: ONE row of the org-scoped team-run LIST — a runs-table row, NOT the full readout. It
    NEVER carries ``results``/``manifest``/``sub_harnesses`` (the repo projects only these columns,
    digging ``team_name`` out of ``manifest.metadata.name`` at query time so the manifest is never
    loaded). ``paused_at`` is the gate role(s) a PAUSED run waits on — what the Approvals inbox
    renders as "waiting on ⟨gate⟩". ``partial`` mirrors ``TeamRunOut`` (a #585 COST_BUDGET halt is
    always partial) so a caller branches on the flag, not the state string."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    state: str
    team_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    paused_at: list[str] = Field(default_factory=list)
    member_status: dict[str, str] = Field(default_factory=dict)
    partial: bool = False
    schedule_id: uuid.UUID | None = None
    cost_tokens: int = 0

    # A real flushed row holds [] / {} / 0 (the column server_defaults); these only coerce a None
    # from a hypothetical pre-migration NULL — fail-soft, never a validation 500 (as TeamRunOut).
    @field_validator("paused_at", mode="before")
    @classmethod
    def _coerce_paused_at(cls, v: Any) -> Any:
        return v if v is not None else []

    @field_validator("member_status", mode="before")
    @classmethod
    def _coerce_member_status(cls, v: Any) -> Any:
        return v if v is not None else {}

    @field_validator("cost_tokens", mode="before")
    @classmethod
    def _coerce_cost_tokens(cls, v: Any) -> Any:
        return v if v is not None else 0

    @model_validator(mode="after")
    def _derive_partial(self) -> TeamRunListItem:
        # #585: a pooled-budget halt is always partial (mirrors TeamRunOut._derive_partial).
        if self.state == "COST_BUDGET":
            self.partial = True
        return self


class TeamRunListOut(BaseModel):
    """#633: one page of the org's team runs + the FULL matching ``total`` (NOT the page length),
    so a runs table can paginate ("showing 1–50 of ``total``"). Mirrors the engine's
    ``{<key>: [...], total}`` list wire convention (as ``{jobs, total}`` / ``{runs, total}`` do)."""

    team_runs: list[TeamRunListItem]
    total: int


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


# ── #635 (C-1): team drafts + the compiler on-ramp ───────────────────────────────────────────────


class CreateTeamDraftRequest(BaseModel):
    """Persist a team DRAFT (#635) — the compile → review → refine loop's editable home. The
    ``manifest`` must be a valid OHM v1.1 Team Harness (the same inbound gate a run applies);
    ``sub_harnesses`` maps member roles to their single-agent sub-harness OHMs (what GO passes
    inline). The response embeds the shared validator's verdict alongside the stored draft."""

    name: str = Field(min_length=1, max_length=256)
    manifest: dict[str, Any]
    sub_harnesses: dict[str, dict[str, Any]] = Field(default_factory=dict)


class TeamDraftFromRunRequest(BaseModel):
    """Seed a draft from a SUCCEEDED compiler run (#635): the run's reviewer member emitted the
    compiled team JSON; the engine peels it, validates it through the SAME importer seam, and
    synthesizes a reasoning-only sub-harness per member. An ineligible run is a curated 422."""

    team_run_id: uuid.UUID
    name: str | None = Field(default=None, max_length=256)


class RefineTeamDraftRequest(BaseModel):
    """Apply ONE typed refine op (#595 vocabulary: add_member | set_fan_out | change_kind |
    add_depends_on) to a draft via ``apply_refine`` — preserve-the-rest guaranteed server-side. A
    blocked op (e.g. an unsurveyed tool) returns ``applied: false`` with the draft untouched.
    ``dry_run`` validates without persisting (the preview path refine-nl hands back to)."""

    edit_op: dict[str, Any]
    dry_run: bool = False


class RefineTeamDraftNlRequest(BaseModel):
    """NL refine (#595): EITHER an ``instruction`` (+ the caller's BYOM ``models[]`` — the
    op-drafter is a real LLM member) to draft a new op, OR an ``op_drafter_run_id`` from a prior
    202 to collect a still-driving draft. ``dry_run`` returns the typed op + verdict WITHOUT
    applying, so the console can preview the structural change before ``refine`` applies it."""

    instruction: str | None = Field(default=None, min_length=1, max_length=4000)
    models: list[dict[str, Any]] = Field(default_factory=list)
    op_drafter_run_id: uuid.UUID | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def _one_of_instruction_or_run(self) -> RefineTeamDraftNlRequest:
        if (self.instruction is None) == (self.op_drafter_run_id is None):
            raise ValueError(
                "supply exactly one of 'instruction' (draft a new op) or"
                " 'op_drafter_run_id' (collect a prior draft)"
            )
        return self


class TeamDraftOut(BaseModel):
    """One stored team draft — the full editable document (manifest + sub_harnesses + version)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    manifest: dict[str, Any]
    sub_harnesses: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("sub_harnesses", mode="before")
    @classmethod
    def _coerce_sub_harnesses(cls, v: Any) -> Any:
        return v if v is not None else {}


class TeamDraftEnvelope(BaseModel):
    """A draft read/write response: the draft PLUS the shared validator's verdict (#593 — one
    validator for import, compile and refine), so the console's validation strip reads
    ``would_block``/``blocking`` for free on every store round-trip. ``report`` is the RENDERED
    dry-run report (the same string the ``core/manifest-validate@1`` tool returns — one validator,
    one shape)."""

    draft: TeamDraftOut
    would_block: bool
    blocking: list[str] = Field(default_factory=list)
    report: str = ""


class TeamDraftListItem(BaseModel):
    """ONE row of the org-scoped draft LIST — a table row, NOT the document: never carries
    ``manifest``/``sub_harnesses`` (the repo projects only these columns, digging
    ``member_count`` out of ``manifest.members`` at query time)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    version: int = 1
    member_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("member_count", mode="before")
    @classmethod
    def _coerce_member_count(cls, v: Any) -> Any:
        return v if v is not None else 0


class TeamDraftListOut(BaseModel):
    """One page of the org's team drafts + the FULL matching ``total`` (NOT the page length) —
    the engine's ``{<key>: [...], total}`` list wire convention, born bounded (WP-10)."""

    team_drafts: list[TeamDraftListItem]
    total: int


class RefineTeamDraftOut(BaseModel):
    """A refine's outcome: the typed ``op`` that was applied (or rejected — returned so the
    console can render the structural preview), ``applied`` (false = the draft is untouched:
    blocked op OR dry_run), the shared validator's verdict, and the (possibly unchanged) draft.
    ``op_drafter_run_id`` is set on the NL path so the drafter run stays auditable."""

    op: dict[str, Any]
    applied: bool
    would_block: bool
    blocking: list[str] = Field(default_factory=list)
    report: str = ""
    draft: TeamDraftOut
    op_drafter_run_id: uuid.UUID | None = None


class RefineTeamDraftNlPendingOut(BaseModel):
    """refine-nl's 202 body: the op-drafter run is still driving — re-call refine-nl with this
    ``op_drafter_run_id`` to collect (or watch the run via the team-run reads)."""

    op_drafter_run_id: uuid.UUID
    status: str = "running"


class CreateCompilerRunRequest(BaseModel):
    """The Describe door (#635, C-1(a) / journey J1): a prose objective (+ the optional
    inputs/constraints/success-criteria fields, folded into the planner's brief server-side) and
    the caller's BYOM ``models[]`` (OHM model bindings; ``config.credential_id`` inside — the
    compiler's members are real LLM agents). The engine assembles the harness-compiler team with
    the #596 seeded catalog and submits it through the SAME team-run path → 202: poll the
    standard ``/v1/engine/team-runs/{id}`` reads, then seed a draft via ``team-drafts/from-run``."""

    objective: str = Field(min_length=1, max_length=8000)
    inputs: dict[str, Any] | None = None
    constraints: str | None = Field(default=None, max_length=4000)
    success_criteria: str | None = Field(default=None, max_length=4000)
    models: list[dict[str, Any]] = Field(min_length=1)
    graph_id: str | None = Field(default=None, max_length=512)
