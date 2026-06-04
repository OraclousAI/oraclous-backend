"""initial auth-service schema: agents, agent_credentials, users, refresh_tokens

Revision ID: 0001_initial
Revises:
Create Date: R3.5-P3-S1

The agent tables (R1) were previously created only via ``Base.metadata.create_all`` in tests; this
is the first migration that lands the full schema for a production (docker) deployment, adding the
user-identity tables (``users``, ``refresh_tokens``) of R3.5 service #3 Slice 1.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("organisation_id", sa.String(), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_agents_organisation_id", "agents", ["organisation_id"])

    op.create_table(
        "agent_credentials",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("organisation_id", sa.String(), nullable=False),
        sa.Column("credential_hash", sa.String(), nullable=False),
        sa.Column("credential_prefix", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_credentials_agent_id", "agent_credentials", ["agent_id"])
    op.create_index(
        "ix_agent_credentials_organisation_id", "agent_credentials", ["organisation_id"]
    )
    op.create_index(
        "ix_agent_credentials_credential_prefix", "agent_credentials", ["credential_prefix"]
    )
    # ADR-012 §1a(a): an active prefix maps to at most one principal, ever.
    op.create_index(
        "ix_agent_credentials_active_prefix_unique",
        "agent_credentials",
        ["credential_prefix"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("default_organisation_id", sa.String(), nullable=False),
        sa.Column(
            "is_email_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("profile_picture", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_users_default_organisation_id", "users", ["default_organisation_id"])
    op.create_index("ix_users_email_unique", "users", ["email"], unique=True)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("organisation_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("jti", sa.String(), nullable=False),
        sa.Column("family_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_refresh_tokens_organisation_id", "refresh_tokens", ["organisation_id"])
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_jti_unique", "refresh_tokens", ["jti"], unique=True)


def downgrade() -> None:
    op.drop_table("refresh_tokens")
    op.drop_table("users")
    op.drop_table("agent_credentials")
    op.drop_table("agents")
