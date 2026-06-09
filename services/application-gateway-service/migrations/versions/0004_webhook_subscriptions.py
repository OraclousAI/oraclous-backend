"""webhook_subscriptions — the R6 Slice 7 gateway webhook-ingress anchor (org-scoped)

Revision ID: 0004_webhook_subscriptions
Revises: 0003_chat
Create Date: 2026-06-09

Maps an opaque inbound webhook id to {org, target published-agent slug, pinned signature scheme, a
reference to the broker-held signing secret}. The secret is NOT here (ADR-008) — only its broker id.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_webhook_subscriptions"
down_revision = "0003_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_slug", sa.String(), nullable=False),
        sa.Column("signature_scheme", sa.String(), nullable=False, server_default="generic"),
        sa.Column("broker_secret_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_webhook_subscriptions_organisation_id",
        "webhook_subscriptions",
        ["organisation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_subscriptions_organisation_id", table_name="webhook_subscriptions")
    op.drop_table("webhook_subscriptions")
