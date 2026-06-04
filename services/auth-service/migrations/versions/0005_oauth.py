"""oauth_accounts + oauth_states (R3.5-P3-S5)

Revision ID: 0005_oauth
Revises: 0004_principal_type
Create Date: R3.5-P3-S5

Linked OAuth provider accounts (tokens encrypted at rest) + single-use PKCE handshake state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_oauth"
down_revision = "0004_principal_type"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "oauth_accounts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("organisation_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("access_token_enc", sa.String(), nullable=False),
        sa.Column("refresh_token_enc", sa.String(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_oauth_accounts_organisation_id", "oauth_accounts", ["organisation_id"])
    op.create_index("ix_oauth_accounts_user_id", "oauth_accounts", ["user_id"])
    op.create_index(
        "ix_oauth_accounts_org_user_provider",
        "oauth_accounts",
        ["organisation_id", "user_id", "provider"],
        unique=True,
    )

    op.create_table(
        "oauth_states",
        sa.Column("state", sa.String(), primary_key=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("code_verifier_enc", sa.String(), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("oauth_states")
    op.drop_table("oauth_accounts")
