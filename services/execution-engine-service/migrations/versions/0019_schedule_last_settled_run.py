"""standing-team recurring-refresh seed — the schedule's last SUCCEEDED team-run (#544, ADR-048 O7)

One additive nullable column on ``engine_schedules`` (RLS-enabled since its create, so it inherits
org-isolation — NO new table, NO enable_rls_on, NO rls_coverage change). It is the SEED for the next
scheduled fire, so a standing team's recurring refresh carries forward the prior fire's records (the
#602 seeded-refresh 5-way delta on a cron) instead of a cold rebuild every tick:

* ``engine_schedules.last_settled_team_run_id`` (UUID, nullable) — the id of the schedule's most
  recent SUCCEEDED team-run. Old rows / a never-succeeded schedule read NULL → the first fire is
  cold (``seed_from_run_id`` NULL, exactly as today). Distinct from ``last_fired_at`` (a window
  timestamp); this is a run-id. No backfill.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_schedule_last_settled_run"
down_revision = "0018_team_run_verdict_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_schedules",
        sa.Column("last_settled_team_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("engine_schedules", "last_settled_team_run_id")
