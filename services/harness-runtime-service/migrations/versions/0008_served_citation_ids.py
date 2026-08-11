"""the run's served citation_id set on harness_executions (Contract #735 §CITE / #743)

Revision ID: 0008_served_citation_ids
Revises: 0007_run_tree_correlation
Create Date: #743

Adds one additive JSONB column, ``served_citation_ids``: the set of ``citation_id``s the PLATFORM
served to that run, accumulated by the loop across every retrieval call and every iteration. It is
what makes rule 2 of §CITE's answer-time gate checkable after the fact — an id a model invented is
not in this list, and there is no other record of what the run was actually handed.

Defaulted to ``'[]'`` and NOT NULL rather than nullable: the gate reads it on every run, and a NULL
would make "served nothing" indistinguishable from "not recorded" at the one moment it matters. A
pre-#743 run is backfilled to the empty list, which is honest — those runs served no citations,
because nothing minted any.

The column holds opaque platform-issued id strings, never source content, so it carries no tenant
data of its own; org isolation is unchanged (reads filter ``organisation_id``, plus the forced-RLS
backstop from 0006).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_served_citation_ids"
down_revision = "0007_run_tree_correlation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harness_executions",
        sa.Column(
            "served_citation_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("harness_executions", "served_citation_ids")
