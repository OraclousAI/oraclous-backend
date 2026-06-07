"""engine_schedules

Revision ID: 0002_engine_schedules
Revises: 0001_engine_jobs
Create Date: R5-S5

Durable schedules that fire harness jobs. Org-scoped (ADR-006). Cron schedules are fired by Celery
Beat; ``last_fired_at`` + the engine_jobs (org, idempotency_key) unique constraint give the
at-least-once firing without duplicates.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002_engine_schedules"
down_revision = "0001_engine_jobs"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "engine_schedules",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=8), nullable=False),
        sa.Column("cron", sa.String(length=128), nullable=True),
        sa.Column("manifest_inline", pg.JSONB(), nullable=True),
        sa.Column("manifest_ref", sa.String(length=512), nullable=True),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index("ix_engine_schedules_organisation_id", "engine_schedules", ["organisation_id"])
    # the beat sweep filters enabled cron schedules across all orgs.
    op.create_index("ix_engine_schedules_type_enabled", "engine_schedules", ["type", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_engine_schedules_type_enabled", table_name="engine_schedules")
    op.drop_index("ix_engine_schedules_organisation_id", table_name="engine_schedules")
    op.drop_table("engine_schedules")
