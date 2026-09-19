"""per-member skip reasons (#1119/#1154)

One additive JSONB column on ``engine_team_runs`` (RLS-enabled since its create, so it inherits
org-isolation — NO new table, NO enable_rls_on, NO rls_coverage change):

* ``member_skip_reasons`` — role -> {"code", "role"} for a member the orchestrator skipped via
  ``run_if`` (``condition_false`` | ``condition_source_missing`` | ``condition_error``), written at
  checkpoint, settle and failure. The graph read (#1154) falls back to ``unrecorded`` for a row
  that predates this migration, which is exactly why the default matters here.

``nullable=False server_default '{}'``, mirroring ``0031``'s ``member_attempt_counts`` — every row
that predates this migration reads ``{}`` rather than NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0032_team_run_skip_reasons"
down_revision = "0031_team_run_member_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("member_skip_reasons", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "member_skip_reasons")
