"""integration_keys — the R6 gateway integration-key store (ADR-019)

Revision ID: 0001_integration_keys
Revises:
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_integration_keys"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("last4", sa.String(length=4), nullable=True),
        sa.Column("bound_agent_slug", sa.String(), nullable=True),
        sa.Column("capability_allow_list", postgresql.JSONB(), nullable=True),
        sa.Column("cors_origins", postgresql.JSONB(), nullable=True),
        sa.Column("rate_limit", sa.Integer(), nullable=True),
        sa.Column("rate_window_seconds", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(bound_agent_slug IS NOT NULL) <> (capability_allow_list IS NOT NULL)",
            name="ck_integration_keys_exactly_one_binding",
        ),
    )
    op.create_index("ix_integration_keys_organisation_id", "integration_keys", ["organisation_id"])
    op.create_index(
        "ix_integration_keys_key_prefix", "integration_keys", ["key_prefix"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_integration_keys_key_prefix", table_name="integration_keys")
    op.drop_index("ix_integration_keys_organisation_id", table_name="integration_keys")
    op.drop_table("integration_keys")
