"""user-seeded inputs on engine_team_runs — fan_out.over seeding (#599)

Revision ID: 0014_team_run_inputs
Revises: 0013_team_run_loop_state
Create Date: #599

Adds the additive, nullable JSONB ``inputs`` column — the team's user-seeded state, threaded to the
orchestrator's ``run_team(state=...)`` so a member's ``fan_out.over: "$.<key>"`` resolves a provided
list (the producer→fan_out leg unwraps an upstream output; this leg supplies the input directly).
Trusted per-run input. Existing rows are NULL (no seeded state). RLS unchanged (the column is
org-scoped by the row).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_team_run_inputs"
down_revision = "0013_team_run_loop_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "inputs")
