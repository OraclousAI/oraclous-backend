"""mid-loop HITL checkpoints

Revision ID: 0004_loop_checkpoints
Revises: 0003_assignments_and_tokens
Create Date: R5-S6

``harness_checkpoints`` parks a run halted at a mid-loop HITL gate so it can be resumed on human
approval. Org-scoped (ADR-006). Everything stored is safe: ``resume_messages`` is the
already-redacted
transcript and ``manifest_doc`` is the OHM descriptor (credential *ids*, never raw secrets).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0004_loop_checkpoints"
down_revision = "0003_assignments_and_tokens"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "harness_checkpoints",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("manifest_doc", pg.JSONB(), nullable=False),
        sa.Column("resume_messages", pg.JSONB(), nullable=False),
        sa.Column("pending_tool_calls", pg.JSONB(), nullable=False),
        sa.Column("approved_tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("resume_cursor", pg.JSONB(), nullable=False),
        sa.Column("redact_patterns", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index(
        "ix_harness_checkpoints_organisation_id", "harness_checkpoints", ["organisation_id"]
    )
    op.create_index("ix_harness_checkpoints_execution_id", "harness_checkpoints", ["execution_id"])


def downgrade() -> None:
    op.drop_index("ix_harness_checkpoints_execution_id", table_name="harness_checkpoints")
    op.drop_index("ix_harness_checkpoints_organisation_id", table_name="harness_checkpoints")
    op.drop_table("harness_checkpoints")
