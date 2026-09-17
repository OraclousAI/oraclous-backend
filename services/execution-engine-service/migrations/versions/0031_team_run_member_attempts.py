"""per-member attempt counts (#1111 decision 4)

One additive JSONB column on ``engine_team_runs`` (RLS-enabled since its create, so it inherits
org-isolation — NO new table, NO enable_rls_on, NO rls_coverage change):

* ``member_attempt_counts`` — role -> attempt count (``1 +`` the in-run recovery retries the member
  spent), written at settle for a member whose harness run FAILED and reported a valid count. A
  run's failure summary reads it to say "after N attempts".

``nullable=False server_default '{}'``, mirroring ``0030``'s ``member_error_codes`` — every row
that predates this migration reads ``{}`` rather than NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0031_team_run_member_attempts"
down_revision = "0030_team_run_member_error_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("member_attempt_counts", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "member_attempt_counts")
