"""standing-team state-binding (THE E8 KEYSTONE) — graph workspace + per-cadence accrual + the
scheduled team-run dedupe (#601, ADR-048 decision 2)

Two additive, backfill-safe column pairs (both tables already have RLS — engine_schedules since
0004, engine_team_runs since its own — so the new columns inherit org-isolation; NO new table, NO
enable_rls_on, NO rls_coverage.yaml change):

* ``engine_schedules.graph_id`` (TEXT, nullable) — the persistent graph workspace a ``team``
  schedule's runs read+write across fires (so fire N+1 sees the state fire N wrote, ADR-040).
* ``engine_schedules.recurring_cost_tokens`` (INTEGER, NOT NULL, default '0') — the per-cadence
  RAW-token ACCRUAL summed across the fires (NOT the run-level pool #585) — the accumulator
  #598's per-period cap reads.
* ``engine_team_runs.schedule_id`` (UUID, nullable) — the schedule that fired this run, so the
  settled cost accrues back; NULL for a direct (request-path) team-run.
* ``engine_team_runs.idempotency_key`` (VARCHAR(255), nullable) + a PARTIAL unique
  ``(organisation_id, idempotency_key) WHERE idempotency_key IS NOT NULL`` — the create-then-enqueue
  dedupe for a scheduled fire (a duplicate Beat tick / fire-now in the same window gets None) that
  does NOT constrain direct team-runs (which leave the key NULL).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_standing_team_binding"
down_revision = "0014_team_run_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("engine_schedules", sa.Column("graph_id", sa.Text(), nullable=True))
    op.add_column(
        "engine_schedules",
        sa.Column("recurring_cost_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "engine_team_runs",
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("engine_team_runs", sa.Column("idempotency_key", sa.String(255), nullable=True))
    # PARTIAL unique: scheduled fires dedupe on (org, key); direct runs (NULL key) are free.
    op.create_index(
        "uq_engine_team_runs_org_idempotency",
        "engine_team_runs",
        ["organisation_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_engine_team_runs_org_idempotency", table_name="engine_team_runs")
    op.drop_column("engine_team_runs", "idempotency_key")
    op.drop_column("engine_team_runs", "schedule_id")
    op.drop_column("engine_schedules", "recurring_cost_tokens")
    op.drop_column("engine_schedules", "graph_id")
