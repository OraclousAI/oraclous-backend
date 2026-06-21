"""adopted-tool schedules + engine_adopted_tool_runs (+ RLS backstop) (#489)

Revision ID: 0009_adopted_tool_schedules
Revises: 0008_team_run_verdict
Create Date: #489 PR-3

PR-3 of #489 lets a cron schedule fire an ADOPTED-TOOL run (a capability-registry instance
``/execute``) in addition to the existing harness-job path.

PART A — additive ALTERs to ``engine_schedules`` (forward-compat; existing rows stay valid). The
``target_kind`` ``server_default='harness_job'`` backfills every existing row so the new fire branch
never mistakes an old harness schedule; ``instance_id``/``input_data`` are nullable (set only on
adopted_tool_run schedules).

PART B — the new ``engine_adopted_tool_runs`` idempotency ledger, mirroring the ``engine_jobs``
``(organisation_id, idempotency_key)`` unique constraint that gives the harness path its
at-least-once-without-duplicates firing. The row is written BEFORE the registry dispatch is enqueued
(in the service ``_fire_one`` branch), so a duplicate same-window fire hits the unique violation and
no second execution is dispatched. ``execution_id`` is the registry ExecutionOut.id, stamped after
dispatch (nullable until then).

RLS in THIS migration (the 0005 team_runs pattern, never deferred): ``engine_adopted_tool_runs`` is
created AFTER the 0004 RLS rollout, so it enables its own org-isolation backstop here — ENABLE +
FORCE row level security + an ``engine_adopted_tool_runs_org_isolation`` policy, via the substrate
``enable_rls_on`` (the single place the RLS shape lives). The org-bound path binds the org so the
policy bites; the migration runs as the OWNER, which owns the table and may run the DDL.
``engine_adopted_tool_runs`` is added to ``rls_coverage.yaml`` in the same change so the coverage
guardrail accounts for it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on
from sqlalchemy.dialects import postgresql as pg

revision = "0009_adopted_tool_schedules"
down_revision = "0008_team_run_verdict"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")
_POLICY_SUFFIX = "_org_isolation"


def upgrade() -> None:
    # PART A — additive, forward-compatible columns on engine_schedules.
    op.add_column(
        "engine_schedules",
        sa.Column(
            "target_kind",
            sa.String(length=16),
            nullable=False,
            server_default="harness_job",
        ),
    )
    op.add_column(
        "engine_schedules",
        sa.Column("instance_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "engine_schedules",
        sa.Column("input_data", pg.JSONB(), nullable=True),
    )

    # PART B — the adopted-tool-run idempotency ledger.
    op.create_table(
        "engine_adopted_tool_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("schedule_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("execution_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index(
        "ix_engine_adopted_tool_runs_organisation_id",
        "engine_adopted_tool_runs",
        ["organisation_id"],
    )
    # at-least-once adopted-tool firing dedupe: an idempotency key is unique within an org.
    op.create_unique_constraint(
        "uq_engine_adopted_tool_runs_org_idempotency",
        "engine_adopted_tool_runs",
        ["organisation_id", "idempotency_key"],
    )
    # Enable the org-isolation RLS backstop (ADR-030) immediately — this table post-dates 0004.
    enable_rls_on(op.get_bind().connection, "engine_adopted_tool_runs")


def downgrade() -> None:
    op.execute(
        f'DROP POLICY IF EXISTS "engine_adopted_tool_runs{_POLICY_SUFFIX}" '
        'ON public."engine_adopted_tool_runs"'
    )
    op.execute('ALTER TABLE public."engine_adopted_tool_runs" NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public."engine_adopted_tool_runs" DISABLE ROW LEVEL SECURITY')
    op.drop_constraint(
        "uq_engine_adopted_tool_runs_org_idempotency",
        "engine_adopted_tool_runs",
        type_="unique",
    )
    op.drop_index(
        "ix_engine_adopted_tool_runs_organisation_id",
        table_name="engine_adopted_tool_runs",
    )
    op.drop_table("engine_adopted_tool_runs")
    op.drop_column("engine_schedules", "input_data")
    op.drop_column("engine_schedules", "instance_id")
    op.drop_column("engine_schedules", "target_kind")
