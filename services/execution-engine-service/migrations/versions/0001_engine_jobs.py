"""initial execution-engine schema: engine_jobs + engine_provenance

Revision ID: 0001_engine_jobs
Revises:
Create Date: R5-S1

First migration for the execution engine. Both tables are org-scoped (ADR-006). ``engine_jobs`` is
the durable job record (the checkpoint state machine around a synchronous harness run); the columns
beyond S1 (retry/timeout/schedule/assignment/idempotency) are created now so later slices add no
ALTERs. ``engine_provenance`` is the sink behind the substrate ProvenanceCollector (the five audit
fields per event), with the owning job embedded in ``resource`` (e.g. ``engine_job:<id>``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001_engine_jobs"
down_revision = None
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "engine_jobs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("manifest_ref", sa.String(length=512), nullable=True),
        sa.Column("manifest_inline", pg.JSONB(), nullable=True),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("harness_execution_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("assignment_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("schedule_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index("ix_engine_jobs_organisation_id", "engine_jobs", ["organisation_id"])
    op.create_index("ix_engine_jobs_harness_execution_id", "engine_jobs", ["harness_execution_id"])
    op.create_index("ix_engine_jobs_schedule_id", "engine_jobs", ["schedule_id"])
    # at-least-once schedule firing dedupe: an idempotency key is unique within an org.
    op.create_unique_constraint(
        "uq_engine_jobs_org_idempotency", "engine_jobs", ["organisation_id", "idempotency_key"]
    )

    op.create_table(
        "engine_provenance",
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
        "ix_engine_provenance_organisation_id", "engine_provenance", ["organisation_id"]
    )
    op.create_index("ix_engine_provenance_resource", "engine_provenance", ["resource"])


def downgrade() -> None:
    op.drop_table("engine_provenance")
    op.drop_constraint("uq_engine_jobs_org_idempotency", "engine_jobs", type_="unique")
    op.drop_index("ix_engine_jobs_schedule_id", table_name="engine_jobs")
    op.drop_index("ix_engine_jobs_harness_execution_id", table_name="engine_jobs")
    op.drop_index("ix_engine_jobs_organisation_id", table_name="engine_jobs")
    op.drop_table("engine_jobs")
