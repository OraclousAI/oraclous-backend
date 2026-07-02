"""ADR-046 (#578): per-gate revision-round counter on engine_team_runs

Revision ID: 0021_team_run_revision_rounds
Revises: 0020_adopted_run_dispatch_claim
Create Date: #578

Adds the additive ``revision_rounds`` column — role -> how many times a human gate has been REVISED
(ADR-046). The revision loop fail-closes to terminal REJECTED once a gate's count exceeds
``max_revisions`` (default 3). Existing rows default to ``{}`` (no gate revised). The
``gate_decisions`` column is UNCHANGED (already JSONB) — its value-shape widening from a bare string
to a GateDecision object is migration-free. RLS unchanged — the column is org-scoped by the row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021_team_run_revision_rounds"
down_revision = "0020_adopted_run_dispatch_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("revision_rounds", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "revision_rounds")
