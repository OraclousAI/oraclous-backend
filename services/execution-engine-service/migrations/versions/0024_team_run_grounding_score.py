"""run-level grounding score — the share of a run's claims that carried a receipt (#642)

One additive column on ``engine_team_runs`` (RLS-enabled since its create, so it inherits
org-isolation — NO new table, NO enable_rls_on, NO rls_coverage change):

* ``engine_team_runs.grounding_score`` (DOUBLE PRECISION, nullable) — Σ grounded / Σ total claims
  across the run's TOOL-DECLARING members, computed at finalize from the per-member grounding counts
  the orchestrator surfaces. NULL when the team declares no tools at all (nothing to ground) and on
  every pre-#642 row. Precedent: ``0018``'s nullable Float ``last_verdict_score``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_team_run_grounding_score"
down_revision = "0023_team_draft_from_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("grounding_score", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "grounding_score")
