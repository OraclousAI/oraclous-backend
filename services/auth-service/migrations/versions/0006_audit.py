"""auth_audit_log (R3.5-P3-S6)

Revision ID: 0006_audit
Revises: 0005_oauth
Create Date: R3.5-P3-S6

Append-only audit of identity events (register / login / oauth-login / invitation-accept).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_audit"
down_revision = "0005_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_audit_log",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("organisation_id", sa.String(), nullable=True),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("actor_type", sa.String(length=24), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("event_metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_auth_audit_log_organisation_id", "auth_audit_log", ["organisation_id"])
    op.create_index("ix_auth_audit_log_actor_id", "auth_audit_log", ["actor_id"])
    op.create_index("ix_auth_audit_log_event", "auth_audit_log", ["event"])


def downgrade() -> None:
    op.drop_table("auth_audit_log")
