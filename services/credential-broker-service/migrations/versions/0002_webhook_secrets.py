"""webhook_secrets — the R6 Slice 7 gateway webhook-ingress signing secrets (org-scoped, AES-GCM)

Revision ID: 0002_webhook_secrets
Revises: 0001_initial
Create Date: 2026-06-09

A per-webhook HMAC signing secret, encrypted at rest with the same AES-256-GCM seam as the user
credentials. Org-scoped (ADR-006); no user/tool owner (a webhook belongs to an org), so it is a
separate table rather than relaxing the personal-credential NOT NULL invariants.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002_webhook_secrets"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "webhook_secrets",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("encrypted_secret", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_webhook_secrets_organisation_id", "webhook_secrets", ["organisation_id"])


def downgrade() -> None:
    op.drop_index("ix_webhook_secrets_organisation_id", table_name="webhook_secrets")
    op.drop_table("webhook_secrets")
