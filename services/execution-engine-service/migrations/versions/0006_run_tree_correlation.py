"""run-tree correlation columns on engine_team_runs (ADR-037 Decision 3 / #471)

Revision ID: 0006_run_tree_correlation
Revises: 0005_engine_team_runs
Create Date: #471

Adds the engine-side run-tree record to ``engine_team_runs``: ``root_execution_id`` (this run's tree
root = the trace_id threaded to every member harness run; minted = the run's own id on first drive)
and ``child_execution_ids`` (the harness execution id of each dispatched member, so the tree is
reassembled from the engine's OWN record — no cross-DB read into the harness). Existing rows are
backfilled ``root_execution_id = id`` (every historical run is a self-rooted tree). RLS is unchanged
(0004/0005 already force it on this table); both columns are org-scoped by the row, so the tree read
can never cross a tenant boundary.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_run_tree_correlation"
down_revision = "0005_engine_team_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("root_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "engine_team_runs",
        sa.Column(
            "child_execution_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Backfill: every existing run is a self-rooted tree (root_execution_id = its own id).
    op.execute("UPDATE engine_team_runs SET root_execution_id = id WHERE root_execution_id IS NULL")


def downgrade() -> None:
    op.drop_column("engine_team_runs", "child_execution_ids")
    op.drop_column("engine_team_runs", "root_execution_id")
