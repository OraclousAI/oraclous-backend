"""schedule-level recurring per-period budget cap (#598, ADR-044 L3 / ADR-048 decision 4b)

Four additive, backfill-safe columns on ``engine_schedules`` (RLS-enabled since 0004, so the new
columns inherit org-isolation — NO new table, NO rls_coverage.yaml change). All-NULL
=> the cap is OFF, so every existing row (and every non-budgeted team) reads clean and takes the
#585/#601 fire path byte-for-byte:

* ``engine_schedules.budget_period`` (VARCHAR(16), nullable) — daily | weekly | monthly; the window
  the recurring accrual (``recurring_cost_tokens``, #601) is checked against and reset at.
* ``engine_schedules.budget_allowance_tokens`` (BIGINT, nullable) — the per-window token ceiling;
  the fleet pauses when the in-window accrual reaches it. (Token-only this slice; USD defers to a
  pricing seam — the engine has no cross-service price fn, ADR-009.)
* ``engine_schedules.budget_window_start`` (TIMESTAMPTZ, nullable) — the current window anchor; a
  fire whose wall-clock window-start exceeds it has rolled → reset the accrual + advance the anchor.
* ``engine_schedules.budget_paused`` (BOOLEAN, NOT NULL, default false) — True when L3 disabled the
  schedule (vs a manual disable), so the boundary re-enable sweep only resumes budget-paused rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_schedule_period_budget"
down_revision = "0015_standing_team_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("engine_schedules", sa.Column("budget_period", sa.String(16), nullable=True))
    op.add_column(
        "engine_schedules",
        sa.Column("budget_allowance_tokens", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "engine_schedules",
        sa.Column("budget_window_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "engine_schedules",
        sa.Column("budget_paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("engine_schedules", "budget_paused")
    op.drop_column("engine_schedules", "budget_window_start")
    op.drop_column("engine_schedules", "budget_allowance_tokens")
    op.drop_column("engine_schedules", "budget_period")
