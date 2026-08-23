"""mid-run signals (#828 + #832): per-member timings + the tree's execution->role map

Two additive JSONB columns on ``engine_team_runs`` (RLS-enabled since its create, so both inherit
org-isolation — NO new table, NO enable_rls_on, NO rls_coverage change):

* ``member_timings`` — role -> {started_at, ended_at | null}, written at dispatch (started_at) and
  at settle (ended_at). Lets a client render a duration / order by time; today no member carries a
  time at all.
* ``child_execution_roles`` — execution_id -> role, so ``GET /tree`` can attribute a child execution
  back to the member that produced it (``child_execution_ids`` stays a flat, unlabeled list).

Both ``nullable=False server_default '{}'``, mirroring the precedent set by ``0012``'s
``member_status`` and ``0013``'s ``loop_state`` — every row that predates this migration reads
``{}`` rather than NULL, so a poll of an old run is not a 500.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0025_team_run_mid_run_signals"
down_revision = "0024_team_run_grounding_score"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("member_timings", JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "engine_team_runs",
        sa.Column("child_execution_roles", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "child_execution_roles")
    op.drop_column("engine_team_runs", "member_timings")
