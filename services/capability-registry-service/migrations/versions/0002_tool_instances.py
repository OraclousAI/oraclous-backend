"""tool_instances table (S3)

Revision ID: 0002_tool_instances
Revises: 0001_initial
Create Date: R3.5-P5-S3

A configured tool instance bound to an org + user. Org-scoped (ADR-006/ORG002); ``capability_id``
FKs the registry descriptor. No ``workflow_id`` (workflows retired, ADR-005).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002_tool_instances"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")

_STATUS = pg.ENUM(
    "PENDING",
    "CONFIGURATION_REQUIRED",
    "READY",
    "RUNNING",
    "SUCCESS",
    "FAILED",
    "PAUSED",
    name="instancestatus",
    create_type=False,
)


def upgrade() -> None:
    _STATUS.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "tool_instances",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "capability_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("capability_descriptors.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "configuration", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("settings", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "credential_mappings",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "required_credentials",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", _STATUS, nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("last_execution_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "execution_count", sa.Numeric(10, 0), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "total_credits_consumed",
            sa.Numeric(10, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
    )
    op.create_index("ix_tool_instances_organisation_id", "tool_instances", ["organisation_id"])
    op.create_index("ix_tool_instances_capability_id", "tool_instances", ["capability_id"])
    op.create_index("ix_tool_instances_user_id", "tool_instances", ["user_id"])
    op.create_index("ix_tool_instances_status", "tool_instances", ["status"])


def downgrade() -> None:
    op.drop_index("ix_tool_instances_status", "tool_instances")
    op.drop_index("ix_tool_instances_user_id", "tool_instances")
    op.drop_index("ix_tool_instances_capability_id", "tool_instances")
    op.drop_index("ix_tool_instances_organisation_id", "tool_instances")
    op.drop_table("tool_instances")
    _STATUS.drop(op.get_bind(), checkfirst=True)
