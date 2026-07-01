"""seeded-refresh — the typed seed reference + the 5-way what-changed delta (#602, ADR-048 dec 3)

Two additive, backfill-safe nullable columns on ``engine_team_runs`` (RLS-enabled since its create,
so the new columns inherit org-isolation — NO new table, NO enable_rls_on, NO rls_coverage change).
Both NULL on every existing row + every non-refresh run, so the seed/delta path is a pure no-op
unless a run is created with ``seed_from_run_id``:

* ``engine_team_runs.seed_from_run_id`` (UUID, nullable) — the NAMED prior run this refreshes from
  (its stored ``results`` are the typed seed). Validated org-scoped + SUCCEEDED-only at create.
* ``engine_team_runs.refresh_delta`` (JSONB, nullable) — the first-class 5-way what-changed delta
  ({added, removed, changed, unchanged, re_confirmed} + counts), computed engine-side at settle.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_team_run_seed_refresh"
down_revision = "0016_schedule_period_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("seed_from_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "engine_team_runs",
        sa.Column("refresh_delta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "refresh_delta")
    op.drop_column("engine_team_runs", "seed_from_run_id")
