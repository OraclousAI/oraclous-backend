"""the member's attempt count on harness_executions (#1111 decision 4)

Revision ID: 0011_execution_attempts
Revises: 0010_execution_leases
Create Date: #1111

Adds one additive integer column, ``attempts``: 1 + the in-run recovery retries the member spent
(final-answer correction turns, transient model retries, transient tool retries), cumulative across
a HITL resume. The engine reads it off the execution response and persists it on its own team-run
record.

NOT NULL with a server default of 1, so every pre-#1111 row is backfilled to a single attempt, which
is honest: no recovery retry was counted before this column existed. The column holds a count, not
tenant content; org isolation is unchanged (reads filter ``organisation_id``, plus the forced-RLS
backstop from 0006).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_execution_attempts"
down_revision = "0010_execution_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harness_executions",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )


def downgrade() -> None:
    op.drop_column("harness_executions", "attempts")
