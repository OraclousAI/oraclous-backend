"""human-actor assignments + token usage

Revision ID: 0003_assignments_and_tokens
Revises: 0002_content_hash
Create Date: R4-S5

``harness_assignments`` records a human-actor task-board assignment (R4 creates it PENDING; durable
resume is R5). ``harness_executions.total_tokens`` records the run's metered LLM token usage.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003_assignments_and_tokens"
down_revision = "0002_content_hash"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.add_column(
        "harness_executions",
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "harness_assignments",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("harness_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("human_role", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index(
        "ix_harness_assignments_organisation_id", "harness_assignments", ["organisation_id"]
    )
    op.create_index("ix_harness_assignments_execution_id", "harness_assignments", ["execution_id"])


def downgrade() -> None:
    op.drop_index("ix_harness_assignments_execution_id", table_name="harness_assignments")
    op.drop_index("ix_harness_assignments_organisation_id", table_name="harness_assignments")
    op.drop_table("harness_assignments")
    op.drop_column("harness_executions", "total_tokens")
