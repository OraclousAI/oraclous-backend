"""graph_id on engine_team_runs — graph-bound team runs (#524, E6 / ADR-040 Decision 7)

Revision ID: 0011_team_run_graph_id
Revises: 0010_team_run_workspace_root
Create Date: #524

Adds the additive, nullable TEXT ``graph_id`` column — the team's bound graph (the trusted per-run
input, cloud-first / graph-primary). Persisted so a resume past a human gate re-binds the SAME graph
to the remaining members. Threaded to every member's graph-tool instance config (knowledge-retriever
/ graph-ingest / find-similar) so the model never invents a UUID. Validated org-scoped at create
(must belong to the caller's org via KGS); NULL → the model supplies graph_id per call / the KGS
org-default graph. RLS unchanged (the column is org-scoped by the row).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_team_run_graph_id"
down_revision = "0010_team_run_workspace_root"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_team_runs",
        sa.Column("graph_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("engine_team_runs", "graph_id")
