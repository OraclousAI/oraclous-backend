"""ADR-043 #552 PR-C: per-loop checkpoint on engine_team_runs

Adds the additive ``loop_state`` column — "<loop_index>" -> {round, started_at, status} — set by
the hybrid conductor (run_team_hybrid) so a genuine loop resumes at a ROUND boundary: the round
counter and the ORIGINAL wall-clock start survive a per-round HITL pause / a mid-loop crash, instead
of restarting the loop from scratch. Existing rows default to ``{}`` (acyclic teams + pre-PR-C runs
never set it). RLS unchanged — the column is org-scoped by the row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013_team_run_loop_state"
down_revision = "0012_team_run_member_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("loop_state", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "loop_state")
