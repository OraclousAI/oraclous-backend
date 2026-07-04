"""team-draft from-run idempotency (#638 concern 3)

One additive, backfill-safe column + a PARTIAL unique index on ``engine_team_drafts`` (already has
RLS since 0022, so the new column inherits org-isolation — NO new table, NO enable_rls_on, NO
rls_coverage.yaml change):

* ``engine_team_drafts.team_run_id`` (UUID, nullable) — the SUCCEEDED compiler run a draft was
  peeled from (``POST /v1/engine/team-drafts/from-run``), else NULL for a directly-created/replaced
  draft.
* a PARTIAL unique ``(organisation_id, team_run_id) WHERE team_run_id IS NOT NULL`` — makes from-run
  idempotent (one draft per (org, run): a reload / a second tab returns the EXISTING draft, never a
  duplicate) WITHOUT constraining direct drafts (which leave the key NULL). Mirrors the
  ``engine_team_runs`` idempotency_key partial unique (0015).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_team_draft_from_run"
down_revision = "0022_engine_team_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_drafts",
        sa.Column("team_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # PARTIAL unique: from-run dedupes on (org, run); direct drafts (NULL team_run_id) are free.
    op.create_index(
        "uq_engine_team_drafts_org_team_run",
        "engine_team_drafts",
        ["organisation_id", "team_run_id"],
        unique=True,
        postgresql_where=sa.text("team_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_engine_team_drafts_org_team_run", table_name="engine_team_drafts")
    op.drop_column("engine_team_drafts", "team_run_id")
