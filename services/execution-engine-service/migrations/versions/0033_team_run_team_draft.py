"""team run source draft (#1163)

Two additive, nullable columns on ``engine_team_runs`` recording which team draft (and which
version of it) a run was started from — mirroring ``app_id``/``schedule_id``/``seed_from_run_id``,
none of which carries a foreign key, so a deleted draft leaves its id in place on the run rather
than nulling it out or blocking the delete:

* ``team_draft_id`` — the draft's id, NULL for a run started any other way.
* ``team_draft_version`` — the draft version the client had loaded when it started the run.

A CHECK (``ck_engine_team_runs_team_draft_pair``) makes "one without the other" impossible at the
database level: both NULL or both set, never a mix.

A partial btree index (``ix_engine_team_runs_org_draft_succeeded``) on
``(organisation_id, team_draft_id, team_draft_version, updated_at)`` restricted to
``state = 'SUCCEEDED' AND team_draft_id IS NOT NULL`` serves the per-draft succeeded-versions read
(``DISTINCT ON (team_draft_version) ORDER BY team_draft_version DESC, updated_at DESC``) and the
"has this draft ever succeeded" existence check, without indexing the RUNNING-state churn from
checkpoint writes.

No FK (matches ``app_id``/``schedule_id``/``seed_from_run_id``), no backfill (an owner decision on
#1163), no RLS change (the table has been FORCE-RLS since migration 0005, and new columns inherit
that automatically).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0033_team_run_team_draft"
down_revision = "0032_team_run_skip_reasons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("team_draft_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "engine_team_runs",
        sa.Column("team_draft_version", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_engine_team_runs_team_draft_pair",
        "engine_team_runs",
        "(team_draft_id IS NULL) = (team_draft_version IS NULL)",
    )
    op.create_index(
        "ix_engine_team_runs_org_draft_succeeded",
        "engine_team_runs",
        ["organisation_id", "team_draft_id", "team_draft_version", "updated_at"],
        postgresql_where=sa.text("state = 'SUCCEEDED' AND team_draft_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_engine_team_runs_org_draft_succeeded", table_name="engine_team_runs")
    op.drop_constraint("ck_engine_team_runs_team_draft_pair", "engine_team_runs", type_="check")
    op.drop_column("engine_team_runs", "team_draft_version")
    op.drop_column("engine_team_runs", "team_draft_id")
