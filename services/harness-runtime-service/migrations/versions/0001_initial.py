"""initial harness-runtime schema: harness_executions + harness_provenance

Revision ID: 0001_initial
Revises:
Create Date: R4-S1

First migration for the harness runtime. Both tables are org-scoped (ADR-006).
``harness_executions`` is the run record (status + step trace); ``harness_provenance`` is the sink
behind the substrate ProvenanceCollector (the five audit fields per step), with the owning execution
embedded in ``resource``.
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
        "harness_executions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("harness_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("harness_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("iterations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("steps", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index(
        "ix_harness_executions_organisation_id", "harness_executions", ["organisation_id"]
    )

    op.create_table(
        "harness_provenance",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("principal", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource", sa.String(length=512), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index(
        "ix_harness_provenance_organisation_id", "harness_provenance", ["organisation_id"]
    )
    op.create_index("ix_harness_provenance_resource", "harness_provenance", ["resource"])


def downgrade() -> None:
    op.drop_index("ix_harness_provenance_resource", table_name="harness_provenance")
    op.drop_index("ix_harness_provenance_organisation_id", table_name="harness_provenance")
    op.drop_table("harness_provenance")
    op.drop_index("ix_harness_executions_organisation_id", table_name="harness_executions")
    op.drop_table("harness_executions")
