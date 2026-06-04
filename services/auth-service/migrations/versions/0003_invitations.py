"""org_invitations (R3.5-P3-S3)

Revision ID: 0003_invitations
Revises: 0002_organisations
Create Date: R3.5-P3-S3

Hashed-token org invitations (T-INVITE): only token_hash + an indexed token_prefix are stored.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_invitations"
down_revision = "0002_organisations"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "org_invitations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("organisation_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "org_role", sa.String(length=16), nullable=False, server_default=sa.text("'member'")
        ),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("token_prefix", sa.String(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("subgraph_grants", sa.JSON(), nullable=True),
        sa.Column("invited_by_user_id", sa.String(), nullable=False),
        sa.Column("accepted_by_user_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_org_invitations_organisation_id", "org_invitations", ["organisation_id"])
    op.create_index("ix_org_invitations_email", "org_invitations", ["email"])
    op.create_index("ix_org_invitations_token_prefix", "org_invitations", ["token_prefix"])
    op.create_index("ix_org_invitations_org_email", "org_invitations", ["organisation_id", "email"])


def downgrade() -> None:
    op.drop_table("org_invitations")
