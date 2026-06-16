"""harness_graph_binding table (Contract G2 / ADR-029)

Revision ID: 0005_harness_graph_binding
Revises: 0004_descriptor_status
Create Date: 2026-06-17

The workspace↔harness binding — a many-to-many curation edge owned by the capability registry
(ADR-029). ``harness_capability_id`` FKs the registry's own ``capability_descriptors``
``ON DELETE CASCADE`` (a harness delete removes its bindings in-service). ``graph_id`` is a plain
UUID with NO cross-service FK (graphs live in knowledge-graph-service; a graph delete is tolerated +
lazily-ignored on read). ``UNIQUE(harness_capability_id, graph_id)`` makes attach idempotent; the
index ``(graph_id, organisation_id)`` serves the agents-for-a-workspace lookup. Org-scoped
(``organisation_id`` NOT NULL — ADR-006); ``created_by`` is the acting principal's user id.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0005_harness_graph_binding"
down_revision = "0004_descriptor_status"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "harness_graph_binding",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "harness_capability_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("capability_descriptors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("graph_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.UniqueConstraint(
            "harness_capability_id", "graph_id", name="uq_harness_graph_binding_pair"
        ),
    )
    op.create_index(
        "ix_harness_graph_binding_harness_capability_id",
        "harness_graph_binding",
        ["harness_capability_id"],
    )
    op.create_index(
        "ix_harness_graph_binding_graph_org",
        "harness_graph_binding",
        ["graph_id", "organisation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_harness_graph_binding_graph_org", "harness_graph_binding")
    op.drop_index("ix_harness_graph_binding_harness_capability_id", "harness_graph_binding")
    op.drop_table("harness_graph_binding")
