"""engine_roundtables

Revision ID: 0003_engine_roundtables
Revises: 0002_engine_schedules
Create Date: R5-S7

``engine_roundtables`` coordinates N actors (agents + humans) over one shared transcript, turn by
turn. Org-scoped (ADR-006). No new execution primitive — agent turns run through the harness, human
turns pause/respond; every result appends to ``transcript`` until ``max_rounds`` complete.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003_engine_roundtables"
down_revision = "0002_engine_schedules"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "engine_roundtables",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("actors", pg.JSONB(), nullable=False),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("current_turn", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="QUEUED"),
        sa.Column("transcript", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("final_output", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index(
        "ix_engine_roundtables_organisation_id", "engine_roundtables", ["organisation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_engine_roundtables_organisation_id", table_name="engine_roundtables")
    op.drop_table("engine_roundtables")
