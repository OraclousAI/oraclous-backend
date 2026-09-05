"""engine_team_runs.app_id — the app a run was started from

Revision ID: 0027_team_run_app_id
Revises: 0026_engine_apps
Create Date: #932 — an app's own run history

Additive and nullable, mirroring ``schedule_id`` (#601) rather than adding a join table: a run
carries the app it came from, so ``GET /v1/engine/apps/{id}/runs`` is one indexed read. NULL for a
run started any other way.

No RLS change. ``engine_team_runs`` keeps its STRICT policy: the app may be one every organisation
can read, but its runs are never shared — two organisations running the same Oraclous-provided app
must not see each other's inputs or results. The index is on ``(organisation_id, app_id)`` for that
reason, since the read is always scoped by both.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0027_team_run_app_id"
down_revision = "0026_engine_apps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs", sa.Column("app_id", pg.UUID(as_uuid=True), nullable=True)
    )
    op.create_index(
        "ix_engine_team_runs_org_app",
        "engine_team_runs",
        ["organisation_id", "app_id"],
        postgresql_where=sa.text("app_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_engine_team_runs_org_app", table_name="engine_team_runs")
    op.drop_column("engine_team_runs", "app_id")
