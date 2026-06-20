"""engine_team_runs (+ RLS backstop)

Revision ID: 0005_engine_team_runs
Revises: 0004_enable_rls
Create Date: R7-E3

``engine_team_runs`` is the DURABLE state of one OHM v1.1 Team Harness execution (the orchestrator's
member-DAG run through the real harness) — the team manifest, the per-role sub-harnesses, the
accumulated per-member results, and the human gate(s) it is paused on. Org-scoped (ADR-006).

This table is created AFTER the RLS rollout (0004), so — unlike 0001-0003 which were RLS'd later by
0004 — it enables its own org-isolation backstop in the same migration (ADR-030): ENABLE + FORCE row
level security + an ``engine_team_runs_org_isolation`` policy, via the substrate ``enable_rls_on``
(the single place the RLS shape lives). The request/driver path binds the org on the org-bound
``oraclous_app`` engine, so the policy bites; the migration runs as the OWNER (``oraclous``), which
owns the table and may run the DDL. ``engine_team_runs`` is added to ``rls_coverage.yaml``
in the same change so the coverage guardrail accounts for it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on
from sqlalchemy.dialects import postgresql as pg

revision = "0005_engine_team_runs"
down_revision = "0004_enable_rls"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")
_POLICY_SUFFIX = "_org_isolation"


def upgrade() -> None:
    op.create_table(
        "engine_team_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest", pg.JSONB(), nullable=False),
        sa.Column("sub_harnesses", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("gate_decisions", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="QUEUED"),
        sa.Column("results", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("paused_at", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index("ix_engine_team_runs_organisation_id", "engine_team_runs", ["organisation_id"])
    # Enable the org-isolation RLS backstop (ADR-030) immediately — this table post-dates 0004.
    enable_rls_on(op.get_bind().connection, "engine_team_runs")


def downgrade() -> None:
    op.execute(
        f'DROP POLICY IF EXISTS "engine_team_runs{_POLICY_SUFFIX}" ON public."engine_team_runs"'
    )
    op.execute('ALTER TABLE public."engine_team_runs" NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public."engine_team_runs" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_engine_team_runs_organisation_id", table_name="engine_team_runs")
    op.drop_table("engine_team_runs")
