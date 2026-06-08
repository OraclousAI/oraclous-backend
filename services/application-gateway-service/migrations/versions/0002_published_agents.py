"""published_agents — the R6 gateway published-agent records (ADR-019, Slice 4)

Revision ID: 0002_published_agents
Revises: 0001_integration_keys
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_published_agents"
down_revision = "0001_integration_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "published_agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("bound_capability_ref", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "slug", name="uq_published_agents_org_slug"),
    )
    op.create_index("ix_published_agents_organisation_id", "published_agents", ["organisation_id"])
    op.create_index("ix_published_agents_slug", "published_agents", ["slug"])


def downgrade() -> None:
    op.drop_index("ix_published_agents_slug", table_name="published_agents")
    op.drop_index("ix_published_agents_organisation_id", table_name="published_agents")
    op.drop_table("published_agents")
