"""initial schema — knowledge_graphs + ingestion_jobs (R3.5-P1-S1)

Baseline matching repositories/models.py. Both tables anchored on organisation_id (ADR-006) plus
the legacy user_id owner. ingestion_jobs is created now so the baseline is whole; it is exercised
from S2 (ingestion spine).

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_graphs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("schema_config", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="active"),
        sa.Column("node_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("relationship_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_graphs_organisation_id", "knowledge_graphs", ["organisation_id"])
    op.create_index("ix_knowledge_graphs_user_id", "knowledge_graphs", ["user_id"])

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("graph_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=True),
        sa.Column("source_content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("extracted_entities", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extracted_relationships", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ingestion_jobs_organisation_id", "ingestion_jobs", ["organisation_id"])
    op.create_index("ix_ingestion_jobs_graph_id", "ingestion_jobs", ["graph_id"])


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_graph_id", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_organisation_id", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_index("ix_knowledge_graphs_user_id", table_name="knowledge_graphs")
    op.drop_index("ix_knowledge_graphs_organisation_id", table_name="knowledge_graphs")
    op.drop_table("knowledge_graphs")
