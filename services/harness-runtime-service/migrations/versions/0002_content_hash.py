"""add content_hash to harness_executions

Revision ID: 0002_content_hash
Revises: 0001_initial
Create Date: R4-S2

The OHM artifact's content hash (SHA-256 of its canonical, signature-excluded bytes) recorded on
each run for provenance/audit. Nullable — runs predating this column have no hash.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_content_hash"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harness_executions", sa.Column("content_hash", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("harness_executions", "content_hash")
