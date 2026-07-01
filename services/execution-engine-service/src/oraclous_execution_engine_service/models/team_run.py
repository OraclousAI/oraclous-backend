"""EngineTeamRun ORM model (models layer).

A team run is ONE execution of an OHM v1.1 Team Harness: its member DAG is driven by the
orchestrator (``oraclous_ohm.orchestrate.run_team``) through the harness, member by member, with
the typed hand-off envelopes threaded by ``depends_on``. This row is the DURABLE state of the run
— the team manifest, the generated per-role sub-harnesses, the accumulated per-member results, and
the human gate(s) it is paused on — so a pause survives across requests and the run is resumable.
Org-scoped (ADR-006); the org-GUC backstop (ADR-030) is enabled on this table in migration 0005.

State machine: ``QUEUED → RUNNING → SUCCEEDED | PAUSED (a human gate) | REJECTED (gate rejected) |
FAILED (a member harness did not succeed)``. ``PAUSED`` is re-drivable: ``advance`` records the gate
decision, returns the row to ``QUEUED``, and re-drives past the now-decided gate.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from typing import Any

from sqlalchemy import Float, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineTeamRun(BaseModel):
    __tablename__ = "engine_team_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # the OHM v1.1 Team Harness manifest (metadata.kind == "team") being run
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # role -> the generated single-agent sub-harness OHM for that member (passed inline to harness)
    sub_harnesses: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # role -> "approve" | "reject" for each human gate that has been decided (seeded + via advance)
    gate_decisions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    # role -> the member's output (the orchestrator's per-member results)
    results: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # the human gate role(s) the run is currently blocked on (empty unless PAUSED)
    paused_at: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ── file-native blackboard (#518; additive, nullable) ─────────────────────────────────────
    # The team's real working tree (the trusted per-run input). Persisted so a resume past a gate
    # re-threads the SAME tree to the remaining members. Validated org-scoped at create (must be
    # under WORKSPACES_ROOT/<org>); NULL → the default per-org scratch sandbox (non-file-native).
    workspace_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ── graph substrate (#524, ADR-040 Decision 7; additive, nullable) ─────────────────────────
    # The team's bound graph (the trusted per-run input). Persisted so a resume past a gate re-binds
    # the SAME graph to the remaining members. Validated org-scoped at create (must belong to the
    # caller's org via KGS); NULL → the model supplies graph_id per call / KGS org-default graph.
    graph_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # #599: user-seeded team state for fan_out.over — a member's fan_out.over: "$.<key>" resolves a
    # provided list (threaded to run_team's ``state``). Trusted per-run input; NULL → no seeded.
    # #602 also rides the seed records under the reserved ``_refresh_seed`` key (the cost lever).
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # #602 (seeded-refresh, ADR-048 dec 3; additive, nullable): the NAMED prior run this refreshes
    # from — its stored ``results`` are the typed seed. Validated org-scoped + SUCCEEDED-only at
    # create (fail-fast 422). NULL → a normal (non-refresh) run: the seed/delta path is a no-op.
    seed_from_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # ── run-tree correlation (ADR-037 Decision 3 / #471; additive, nullable) ──────────────────
    # root_execution_id is this run's tree root = the trace_id threaded to every member harness run
    # (minted = this run's id on first drive; STABLE across resume — read-if-NULL). The list
    # child_execution_ids accumulates each dispatched member's harness execution id, so the tree is
    # reassembled from the engine's own record (no cross-DB query). Both org-scoped by the row.
    root_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    child_execution_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # ── O4 metering (ADR-037 Decision 5 / #472; additive) ─────────────────────────────────────
    # Accumulated RAW token cost of this run = Σ the member harness executions' total_tokens (read
    # from each dispatch response — the harness's existing metering, ADR-009 raw counts, never a
    # price). Read by the O4 light-status surface; usd is priced read-time, never persisted here.
    cost_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # ── flow-evaluation verdict (ADR-037 / #477; additive, nullable) ──────────────────────────
    # The typed Verdict / OHMBatteryVerdict (model_dump) from grading the completed run at the gate
    # — pass/score/recommended_action/failures. PRODUCED + STORED here, surfaced read-side; the
    # run STATE is never branched on it (consuming it = re-dispatch = E8, out of scope). NULL until
    # graded; a fail-closed verdict (pass=false) is stored if the judge is unreachable/unconfigured.
    verdict: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # ── seeded-refresh 5-way delta (#602, ADR-048 dec 3; additive, nullable) ──────────────────────
    # The first-class what-changed delta (the refresh's contract, not a side effect): per-record
    # {added, removed, changed, unchanged, re_confirmed} + counts, computed engine-side at settle by
    # comparing this run's records to the seed run's (identity + evidence fingerprint). NULL on a
    # non-refresh run (seed_from_run_id NULL); surfaced read-side on TeamRunOut beside the verdict.
    refresh_delta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # ── per-member terminal status (ADR-042 / #551; additive) ──────────────────────────────────
    # role -> "succeeded"|"failed"|"blocked"|"skipped"|"budget_skipped" (#585)|"partial" (#587) —
    # the orchestrator's per-member result.
    # A team run is SUCCEEDED only when EVERY member delivered; a FAILED run carries the failed +
    # blocked members here, and the re-run re-drives exactly those (seeding the succeeded ones via
    # ``completed``). Empty until the first drive records it.
    member_status: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # ── per-loop checkpoint (ADR-043 #552 PR-C; additive) ─────────────────────────────────────
    # "<loop_index>" -> {round, started_at, status} — set by the hybrid conductor so a loop resumes
    # at a ROUND boundary (the round counter + the ORIGINAL wall-clock start survive a HITL pause /
    # crash, instead of restarting the loop). Empty for an acyclic team or until a loop runs.
    loop_state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # ── #604 closed-loop verdict-consumption (ADR-048 dec 5; additive) ─────────────────────────
    # The CROSS-re-dispatch loop state (distinct from loop_state, the #552/#553 WITHIN-run round
    # checkpoint): how many times the settled verdict re-dispatched this run (re_task), + the prior
    # verdict's score + fingerprint — the livelock basis (same below-threshold shape recurring with
    # no score gain → escalate). 0/NULL on a run that never re-dispatched.
    re_dispatch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_verdict_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_verdict_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The CONTROL discriminator for a verdict-escalation PAUSE — "verdict" when a settled below-
    # threshold run was escalated for HITL (``advance`` re-tasks it, never a blind re-drive), NULL
    # for a normal mid-drive human gate. A DEDICATED column (not an overloaded ``paused_at`` member-
    # role) so a tenant that names a member the escalation sentinel can never hijack the resume path
    # (review F1-sentinel). NULL on every pre-#604 / non-escalated run.
    escalation_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # ── #601 standing-team binding (additive, nullable) ───────────────────────────────────────
    # The schedule that FIRED this run (a standing team) — so its settled ``cost_tokens`` accrues
    # back into the schedule's per-cadence accumulator; NULL for a direct (request-path) team-run.
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # The at-least-once dedupe key for a scheduled fire — ``f"{schedule_id}:{window}"``. A PARTIAL
    # unique (org, idempotency_key) WHERE NOT NULL makes the create-before-enqueue fire idempotent
    # (a duplicate Beat tick / fire-now in the same window gets None) WITHOUT constraining direct
    # team-runs (which leave it NULL).
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "uq_engine_team_runs_org_idempotency",
            "organisation_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )
