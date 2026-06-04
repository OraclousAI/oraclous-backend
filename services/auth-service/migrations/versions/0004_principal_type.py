"""agent_credentials.principal_type discriminator (R3.5-P3-S4)

Revision ID: 0004_principal_type
Revises: 0003_invitations
Create Date: R3.5-P3-S4

Adds a ``principal_type`` column so the machine-principal credential path mints either an ``agent``
(default) or a ``service_account`` token — no redundant parallel stack. Existing rows default to
``agent`` (behaviour-preserving).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_principal_type"
down_revision = "0003_invitations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_credentials",
        sa.Column(
            "principal_type",
            sa.String(),
            nullable=False,
            server_default=sa.text("'agent'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_credentials", "principal_type")
