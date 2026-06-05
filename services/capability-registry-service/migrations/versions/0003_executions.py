"""executions table (S4)

Revision ID: 0003_executions
Revises: 0002_tool_instances
Create Date: R3.5-P5-S4

Provenance of every tool dispatch. Org-scoped (ADR-006/ORG002). ``credential_refs`` records the
credential types/scopes used, never the secret material.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003_executions"
down_revision = "0002_tool_instances"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")

_STATUS = pg.ENUM(
    "QUEUED",
    "RUNNING",
    "SUCCESS",
    "FAILED",
    name="executionstatus",
    create_type=False,
)


def upgrade() -> None:
    _STATUS.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "executions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("capability_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("status", _STATUS, nullable=False),
        sa.Column("input_data", pg.JSONB(), nullable=True),
        sa.Column("output_data", pg.JSONB(), nullable=True),
        sa.Column("credential_refs", pg.JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_type", sa.String(length=100), nullable=True),
        sa.Column(
            "credits_consumed", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("processing_time_ms", sa.Numeric(10, 0), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
    )
    op.create_index("ix_executions_organisation_id", "executions", ["organisation_id"])
    op.create_index("ix_executions_instance_id", "executions", ["instance_id"])
    op.create_index("ix_executions_user_id", "executions", ["user_id"])
    op.create_index("ix_executions_status", "executions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_executions_status", "executions")
    op.drop_index("ix_executions_user_id", "executions")
    op.drop_index("ix_executions_instance_id", "executions")
    op.drop_index("ix_executions_organisation_id", "executions")
    op.drop_table("executions")
    _STATUS.drop(op.get_bind(), checkfirst=True)
