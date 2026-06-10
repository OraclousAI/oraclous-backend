"""org_data_keys — per-org DEK wraps for envelope encryption (ADR-020, R7-SEC S5)

Revision ID: 0003_org_data_keys
Revises: 0002_webhook_secrets
Create Date: 2026-06-10

One row per organisation: the KEK-wrapped data-encryption key. The plaintext DEK is never stored.
``organisation_id`` is UNIQUE (the per-tenant boundary). Additive + non-destructive — existing v1
ciphertext is untouched; the DEK is created lazily on an org's first v2 write.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003_org_data_keys"
down_revision = "0002_webhook_secrets"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "org_data_keys",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("wrapped_dek", sa.String(), nullable=False),
        sa.Column("kek_provider", sa.String(), nullable=False),
        sa.Column("kek_key_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # UNIQUE org -> one DEK per tenant (the lazy-create race re-reads on this constraint)
    op.create_index(
        "ux_org_data_keys_organisation_id", "org_data_keys", ["organisation_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ux_org_data_keys_organisation_id", table_name="org_data_keys")
    op.drop_table("org_data_keys")
