"""knowledge_graphs.system_kind — mark a reserved system-owned graph (#332 / ADR-027 §5)

The lazily-created org-default agent-memory graph must NEVER resolve to (or collide with) a
user-created graph that happens to share its display name. This adds a nullable `system_kind`
marker (NULL for a user graph; a reserved value such as `agent_memory` for a system graph) plus an
org-scoped PARTIAL unique index so at most ONE system graph of each kind exists per org. The index
makes the lazy find-or-create race-safe: a concurrent first run that loses the insert race hits the
unique violation and re-reads the winner instead of creating a duplicate default graph.

Revision ID: 0006_graph_system_kind
Revises: 0005_resolution_other_graph
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_graph_system_kind"
down_revision: str | None = "0005_resolution_other_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_graphs",
        sa.Column("system_kind", sa.String(length=50), nullable=True),
    )
    op.create_index(
        "uq_knowledge_graph_system_kind",
        "knowledge_graphs",
        ["organisation_id", "system_kind"],
        unique=True,
        postgresql_where=sa.text("system_kind IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_knowledge_graph_system_kind", table_name="knowledge_graphs")
    op.drop_column("knowledge_graphs", "system_kind")
