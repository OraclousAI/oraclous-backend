"""workspace_root on engine_team_runs — file-native team runs (#518, E6 / ADR-040)

Revision ID: 0010_team_run_workspace_root
Revises: 0009_adopted_tool_schedules
Create Date: #518

Adds the additive, nullable TEXT ``workspace_root`` column — the team's real git-markdown working
tree (the trusted per-run input). Persisted so a resume past a human gate re-threads the SAME tree
to the remaining members. Threaded to every member's file-tool instance config so the file tools
read/write in place (org-confined by the registry sandbox guard, #517). NULL → the default per-org
scratch sandbox (a non-file-native team). RLS unchanged (the column is org-scoped by the row).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_team_run_workspace_root"
down_revision = "0009_adopted_tool_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("workspace_root", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "workspace_root")
