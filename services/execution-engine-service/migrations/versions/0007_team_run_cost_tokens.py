"""O4 metering: accumulated cost_tokens on engine_team_runs (ADR-037 Decision 5 / #472)

Revision ID: 0007_team_run_cost_tokens
Revises: 0006_run_tree_correlation
Create Date: #472

Adds the additive ``cost_tokens`` column — the accumulated RAW token cost of a team run (Σ the
member harness executions' ``total_tokens``, read from each dispatch response; ADR-009 raw counts,
never a price). Read by the O4 light-status surface. Existing rows default to 0. RLS unchanged (the
column is org-scoped by the row); the team-run progress itself is COMPUTED at read time from the
run-tree (member-completion) — no second progress field is stored (ADR-037 anti-goal F).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_team_run_cost_tokens"
down_revision = "0006_run_tree_correlation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("cost_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "cost_tokens")
