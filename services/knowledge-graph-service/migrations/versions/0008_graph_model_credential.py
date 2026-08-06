"""knowledge_graphs.model_credential_id — a graph's own BYOM model credential (#724)

Every model call over a graph's content (entity extraction, community summarisation, vision,
embeddings, schema synthesis) resolves the ORGANISATION's credential rather than a key of the
platform's. This column is the per-graph OVERRIDE of that default: NULL means "use the org
default", a value pins this graph to its own credential.

It exists because a graph is the natural unit at which the choice varies. A sensitive graph can be
pinned to a different model from the org's everyday one, which is the ADR-008 §3.6 concern read
forward: operator separation is weaker if every graph in an org must share one model.

Additive and non-destructive: existing graphs get NULL and follow the org default, which is the
behaviour they would have had anyway.

Revision ID: 0008_graph_model_credential
Revises: 0007_enable_rls
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_graph_model_credential"
down_revision: str | None = "0007_enable_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No FK to the broker's user_credentials: it is another service's table (and another database
    # in a split deploy). The id is validated by RESOLUTION instead, which fails closed when the
    # credential is gone or belongs to another org.
    op.add_column(
        "knowledge_graphs",
        sa.Column("model_credential_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_graphs", "model_credential_id")
