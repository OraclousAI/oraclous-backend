"""run-tree correlation columns on harness_executions (ADR-037 Decision 3 / #471)

Revision ID: 0007_run_tree_correlation
Revises: 0006_enable_rls
Create Date: #471

Adds the two additive, nullable correlation columns that let a multi-service team run be reassembled
into one tree: ``trace_id`` (the run-tree root id — every execution in the tree shares it) and
``parent_execution_id`` (the dispatching member's execution; NULL at the root). ``trace_id`` is
indexed for the tree read. Existing rows are backfilled ``trace_id = id`` so every historical run is
a self-rooted tree of one (never NULL → never an orphan in the read). Org isolation is unchanged:
reads still filter ``organisation_id`` (+ the forced-RLS backstop from 0006), so a ``trace_id`` is
an opaque correlation value that can never cross a tenant boundary.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_run_tree_correlation"
down_revision = "0006_enable_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harness_executions",
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "harness_executions",
        sa.Column("parent_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Backfill: every existing run becomes a self-rooted tree of one (trace_id = its own id).
    op.execute("UPDATE harness_executions SET trace_id = id WHERE trace_id IS NULL")
    op.create_index("ix_harness_executions_trace_id", "harness_executions", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_harness_executions_trace_id", table_name="harness_executions")
    op.drop_column("harness_executions", "parent_execution_id")
    op.drop_column("harness_executions", "trace_id")
