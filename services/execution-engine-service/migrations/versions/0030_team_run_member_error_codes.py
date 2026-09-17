"""per-member curated error codes (#1108 ruling 2c)

One additive JSONB column on ``engine_team_runs`` (RLS-enabled since its create, so it inherits
org-isolation — NO new table, NO enable_rls_on, NO rls_coverage change):

* ``member_error_codes`` — role -> curated error token (e.g. ``llm_credential_rejected``), written
  at settle for a member whose harness run FAILED with a curated ``error_type``. Only allow-listed
  tokens are stored, never provider text, so a caller can map a member's failure to a typed refusal.

``nullable=False server_default '{}'``, mirroring ``0025``'s ``member_timings`` /
``child_execution_roles`` — every row that predates this migration reads ``{}`` rather than NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0030_team_run_member_error_codes"
down_revision = "0029_provenance_context_hashes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("member_error_codes", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "member_error_codes")
