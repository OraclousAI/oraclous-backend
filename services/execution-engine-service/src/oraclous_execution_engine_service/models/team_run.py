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

from sqlalchemy import Integer, String, Text
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
    # ── per-member terminal status (ADR-042 / #551; additive) ──────────────────────────────────
    # role -> "succeeded" | "failed" | "blocked" | "skipped" (the orchestrator's per-member result).
    # A team run is SUCCEEDED only when EVERY member delivered; a FAILED run carries the failed +
    # blocked members here, and the re-run re-drives exactly those (seeding the succeeded ones via
    # ``completed``). Empty until the first drive records it.
    member_status: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
