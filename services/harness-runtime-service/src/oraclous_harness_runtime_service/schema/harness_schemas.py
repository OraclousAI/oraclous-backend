"""Harness execution DTOs (schema layer) — Pydantic request/response models only.

``organisation_id`` is never inbound (ORG001) — it is resolved from the authenticated principal in
the route. The manifest is supplied inline (raw YAML or an already-parsed object); ``manifest_ref``
resolution against the registry lands in slice 2.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

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
    # file-native blackboard (#518): the team run's trusted working tree. The harness sets it on
    # file-tool instance's config so the member's file tools operate in place on it (org-confined by
    # the capability-registry sandbox guard, #517). None → the default per-org scratch sandbox.
    workspace_root: str | None = None
    # graph substrate (#524): the team run's bound graph. The harness sets it on each instance's
    # config so the graph tools (knowledge-retriever / graph-ingest / find-similar) target it
    # (org-scoped at create + by KGS RLS). None → the model supplies graph_id / KGS org-default.
    graph_id: str | None = None
    # team-scope blackboard (#513): the stable team identity (team-manifest id). The post-run memory
    # hook writes ``scope=team`` under it (into the bound graph) + the loop reads the team's memory.
    # None → a single-agent run (legacy ``scope=agent``).
    team_id: str | None = None
    # #728: WHO is writing. Bound by the ENGINE at dispatch (producer_kind / team_run_id /
    # member_role / team_id / ordinal) and set on each instance's configuration, so an artifact the
    # run writes records its producer and two members can never collide onto one graph node. The
    # harness adds ``execution_id`` before binding it. None → a single-agent or user-driven run,
    # which the KGS reads as a user upload and keeps filename-based document keying (#522).
    producer: dict[str, Any] | None = None
    # Hierarchy of Truth (#538): the team's declared precedence (order high→low + graph mode). The
    # harness binds it on each instance's config so the knowledge-retriever ranks a member's in-loop
    # read canonical-first (#536). None/empty → no precedence applied (read unchanged).
    precedence_order: list[str] | None = None
    graph_authoritative: bool = False
    # #576: the dispatching member's user-set runtime SAFETY CAP (resolved + clamped engine-side to
    # <= the team-pooled total). The harness applies it as the per-member token / tool-call ceiling,
    # overriding the policy tier. None → the policy tier stands (single-agent / no per-member cap).
    max_tokens: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    # #587: the member's budget-exhaustion behaviour — "degrade" finishes a budget-gated loop with a
    # flagged PARTIAL; None ⇒ the harness default "escalate" (back-compat).
    on_exhaustion: Literal["escalate", "degrade"] | None = Field(default=None)
    # #853: the dispatching member declared that its structured document must PARSE. The loop checks
    # a JSON graph-ingest call before dispatching it and grants exactly one repair turn on a
    # malformed one. False ⇒ unchanged behaviour (every pre-#853 caller omits the key).
    requires_valid_json: bool = Field(default=False)

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
    # #641: the LLM's id for the tool call this step records — the receipt a member's claim cites.
    # None for an LLM/gate step AND for traces persisted before #641 (back-compat, no migration).
    tool_call_id: str | None = None


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
    # #743 (Contract #735 §CITE): the citation_ids the PLATFORM served to this run. A citation in
    # the answer resolves only if it is in here — that is rule 2, and it is the property that tells
    # a real source from an invented one. Opaque platform-issued ids, never source content.
    served_citation_ids: list[str] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def driving_signals(self) -> list[dict[str, Any]]:
        """#642 structural receipts: one grounded claim per ok tool step of this run's OWN trace.

        Derived from the trace, never from the model — a model's self-written receipt is the least
        trustworthy source for what it did, and whether it emits one at all is a coin flip (live
        runs ``8097a667`` vs ``afc3b2c4``). The engine takes this list over anything parsed from
        the answer, so the grounding grade is deterministic: tools ran ok ⇒ receipts exist;
        no ok call (including a pre-#641 trace with no ids) ⇒ empty ⇒ the grade fails closed.
        """
        return [
            {
                "signal": f"tool {s.name} succeeded",
                "value": True,
                "source_tool_call_id": s.tool_call_id,
            }
            for s in self.steps
            if s.kind is StepKind.TOOL and s.status == "ok" and s.tool_call_id
        ]


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
