"""entity_resolutions.other_graph_id — record a cross-graph verdict's SECOND graph (#330 / ADR-026)

A cross-graph SAME_AS verdict spans TWO graphs. The original audit row carried only `graph_id`
(the path graph), so the pair's second graph was lost and a cross-graph LINK could not be told from
an in-graph fold in the audit. This adds the nullable `other_graph_id` column: NULL for an in-graph
verdict (fold/suppress within one graph), the second graph id for a cross-graph LINK/suppress.

The cross-graph verdict is now keyed SYMMETRICALLY on the canonicalised pair (the audit row's
`graph_id` is always the lexicographically-smaller of the two graph ids, `other_graph_id` the
larger), so a verdict submitted from EITHER direction resolves to the SAME row — SAME_AS and
NOT_SAME_AS can no longer coexist for one pair, and a conflicting verdict from the other direction
is a 409, not a 404.

Revision ID: 0005_resolution_other_graph
Revises: 0004_entity_resolutions
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_resolution_other_graph"
down_revision: str | None = "0004_entity_resolutions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "entity_resolutions",
        sa.Column("other_graph_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("entity_resolutions", "other_graph_id")
