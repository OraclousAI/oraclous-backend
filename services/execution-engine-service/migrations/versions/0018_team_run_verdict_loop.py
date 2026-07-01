"""closed-loop verdict-consumption — the cross-re-dispatch loop state (#604, ADR-048 decision 5)

Three additive columns on ``engine_team_runs`` (RLS-enabled since its create, so they inherit
org-isolation — NO new table, NO enable_rls_on, NO rls_coverage change). They carry the CROSS-run
loop state the settle-time verdict branch reads (distinct from ``loop_state``, the #552/#553
WITHIN-run round checkpoint, which is off-limits):

* ``engine_team_runs.re_dispatch_count`` (INTEGER, NOT NULL, default 0) — how many times the settled
  verdict re-dispatched this run (re_task); the MAX_RE_DISPATCHES ceiling reads it. Old rows
  0 (never re-dispatched).
* ``engine_team_runs.last_verdict_score`` (DOUBLE PRECISION, nullable) — the prior re-dispatch's
  score, the livelock improvement basis.
* ``engine_team_runs.last_verdict_fingerprint`` (VARCHAR(256), nullable) — the prior below-threshold
  shape; the SAME fingerprint recurring with no score gain → escalate (livelock).
* ``engine_team_runs.escalation_kind`` (VARCHAR(32), nullable) — the CONTROL discriminator for a
  verdict-escalation PAUSE ("verdict" → ``advance`` re-tasks it; NULL → a normal human gate). A
  dedicated column so a tenant-named member can never hijack the verdict-escalation resume path.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_team_run_verdict_loop"
down_revision = "0017_team_run_seed_refresh"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("re_dispatch_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "engine_team_runs",
        sa.Column("last_verdict_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "engine_team_runs",
        sa.Column("last_verdict_fingerprint", sa.String(256), nullable=True),
    )
    op.add_column(
        "engine_team_runs",
        sa.Column("escalation_kind", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "escalation_kind")
    op.drop_column("engine_team_runs", "last_verdict_fingerprint")
    op.drop_column("engine_team_runs", "last_verdict_score")
    op.drop_column("engine_team_runs", "re_dispatch_count")
