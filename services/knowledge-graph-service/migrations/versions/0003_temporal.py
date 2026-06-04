"""ingestion_jobs temporal passthrough columns (R3.5-P1-S5)

valid_from / valid_to / event_time carry bitemporal validity from an ingest onto the projected
entity nodes (the recipe engine stamps them; ontology coercion is independent).

Revision ID: 0003_temporal
Revises: 0002_recipes
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_temporal"
down_revision: str | None = "0002_recipes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ingestion_jobs", sa.Column("valid_from", sa.String(length=64), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("valid_to", sa.String(length=64), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("event_time", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "event_time")
    op.drop_column("ingestion_jobs", "valid_to")
    op.drop_column("ingestion_jobs", "valid_from")
