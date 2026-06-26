"""ADR-042 (#551): per-member terminal status on engine_team_runs

Revision ID: 0012_team_run_member_status
Revises: 0011_team_run_graph_id
Create Date: #551

Adds the additive ``member_status`` column — role -> "succeeded" | "failed" | "blocked" | "skipped"
(the orchestrator's per-member verdict, ADR-042). A team run is SUCCEEDED only when EVERY member
delivered; this column is what makes the failed/blocked members re-runnable (the re-run re-drives
them, seeding the succeeded members via ``completed``). Existing rows default to ``{}``. RLS
unchanged — the column is org-scoped by the row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_team_run_member_status"
down_revision = "0011_team_run_graph_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("member_status", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "member_status")
