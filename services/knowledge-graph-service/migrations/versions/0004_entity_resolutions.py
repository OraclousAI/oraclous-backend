"""entity_resolutions HITL audit table (#279)

Records WHO approved/rejected WHICH SAME_AS_CANDIDATE pair, WHEN — a governance-relevant mutation.
The (organisation_id, graph_id, candidate_id) unique key makes a verdict idempotent (replay = no-op)
and surfaces a concurrent second-reviewer conflict.

Revision ID: 0004_entity_resolutions
Revises: 0003_temporal
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_entity_resolutions"
down_revision: str | None = "0003_temporal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_resolutions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("graph_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("node_id_a", sa.String(length=128), nullable=False),
        sa.Column("node_id_b", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("canonical_node_id", sa.String(length=128), nullable=True),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "organisation_id",
            "graph_id",
            "candidate_id",
            name="uq_entity_resolution_candidate",
        ),
    )
    op.create_index(
        "ix_entity_resolutions_organisation_id", "entity_resolutions", ["organisation_id"]
    )
    op.create_index("ix_entity_resolutions_graph_id", "entity_resolutions", ["graph_id"])


def downgrade() -> None:
    op.drop_index("ix_entity_resolutions_graph_id", table_name="entity_resolutions")
    op.drop_index("ix_entity_resolutions_organisation_id", table_name="entity_resolutions")
    op.drop_table("entity_resolutions")
