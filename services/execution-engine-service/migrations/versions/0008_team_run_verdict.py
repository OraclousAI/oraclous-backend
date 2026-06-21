"""flow-evaluation verdict on engine_team_runs (ADR-037 / #477)

Revision ID: 0008_team_run_verdict
Revises: 0007_team_run_cost_tokens
Create Date: #477

Adds the additive, nullable JSONB ``verdict`` column — the typed Verdict / OHMBatteryVerdict from
grading a completed team run at the gate (pass/score/recommended_action/failures). PRODUCED + STORED
here and surfaced read-side; the run state is never branched on it (consuming it = re-dispatch = E8,
out of scope). NULL until graded. RLS unchanged (the column is org-scoped by the row).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_team_run_verdict"
down_revision = "0007_team_run_cost_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("verdict", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "verdict")
