"""organisations + org_members (R3.5-P3-S2)

Revision ID: 0002_organisations
Revises: 0001_initial
Create Date: R3.5-P3-S2

Adds the tenant organisation table (the scope-root) and the membership edge (user ↔ org + role)
that backs active-org selection and the governance MembershipResolver.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_organisations"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "organisations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("logo_url", sa.String(length=512), nullable=True),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "status", sa.String(length=50), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_organisations_owner_user_id", "organisations", ["owner_user_id"])
    op.create_index("ix_organisations_slug_unique", "organisations", ["slug"], unique=True)

    op.create_table(
        "org_members",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("organisation_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column(
            "org_role", sa.String(length=16), nullable=False, server_default=sa.text("'member'")
        ),
        sa.Column("since", sa.TIMESTAMP(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_org_members_organisation_id", "org_members", ["organisation_id"])
    op.create_index("ix_org_members_user_id", "org_members", ["user_id"])
    op.create_index(
        "ix_org_members_org_user_unique", "org_members", ["organisation_id", "user_id"], unique=True
    )


def downgrade() -> None:
    op.drop_table("org_members")
    op.drop_table("organisations")
