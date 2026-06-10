"""capability_descriptors.status — the supply-chain approval gate (R6 MCP-import)

Revision ID: 0004_descriptor_status
Revises: 0003_executions
Create Date: 2026-06-10

Additive + non-destructive: a new ``status`` column defaulting to ``active`` — so every existing
descriptor (and every first-party/built-in registration) stays executable. An imported external MCP
tool is created ``pending_approval`` and only becomes ``active`` on an admin's approval.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_descriptor_status"
down_revision = "0003_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "capability_descriptors",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
    )


def downgrade() -> None:
    op.drop_column("capability_descriptors", "status")
