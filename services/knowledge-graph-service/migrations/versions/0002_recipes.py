"""recipes table + ingestion_jobs.recipe_id (R3.5-P1-S3)

Recipes are versioned data (id, version, organisation_id PK); a new version is a new row. The
ingestion_jobs.recipe_id links a structured ingest to a stored recipe (null -> default recipe).

Revision ID: 0002_recipes
Revises: 0001_initial
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_recipes"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ingestion_jobs", sa.Column("recipe_id", sa.String(length=255), nullable=True))
    op.create_table(
        "recipes",
        sa.Column("id", sa.String(length=255), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("shape_signature", sa.Text(), nullable=False),
        sa.Column("concern", sa.String(length=255), nullable=False),
        sa.Column("recipe_json", sa.JSON(), nullable=False),
        sa.Column("authored_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_recipes_lookup",
        "recipes",
        ["organisation_id", "source_type", "shape_signature", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_recipes_lookup", table_name="recipes")
    op.drop_table("recipes")
    op.drop_column("ingestion_jobs", "recipe_id")
