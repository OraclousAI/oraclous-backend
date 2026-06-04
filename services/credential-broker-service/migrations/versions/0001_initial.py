"""initial credential-broker schema: user_credentials, delegated_tokens

Revision ID: 0001_initial
Revises:
Create Date: R3.5-P4-S0

First migration for the production (docker) deployment. Both tables are org-scoped (ADR-006);
``user_credentials.encrypted_cred`` holds AES-256-GCM ciphertext; ``delegated_tokens`` stores only a
SHA-256 hash + prefix (never the raw bearer).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "user_credentials",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("encrypted_cred", sa.String(), nullable=False),
        sa.Column("cred_type", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_user_credentials_org_user_tool",
        "user_credentials",
        ["organisation_id", "user_id", "tool_id"],
    )

    op.create_table(
        "delegated_tokens",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("scopes", pg.ARRAY(sa.String()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("token_prefix", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_delegated_tokens_token_prefix", "delegated_tokens", ["token_prefix"])
    op.create_index(
        "ix_delegated_tokens_org_prefix", "delegated_tokens", ["organisation_id", "token_prefix"]
    )


def downgrade() -> None:
    op.drop_table("delegated_tokens")
    op.drop_table("user_credentials")
