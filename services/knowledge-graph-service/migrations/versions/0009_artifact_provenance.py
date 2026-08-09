"""ingestion_jobs provenance — which member of which run produced an artifact (#728)

An artifact had no producer. ``ingestion_jobs`` recorded the graph, the filename and the content,
and nothing about who wrote it, so a run's outputs were indistinguishable from each other and from
a user's upload. Run ``dc167d8e`` landed 7 artifacts, every one named ``inline.txt``, and the graph
kept 1: the lexical document node is keyed on that name, so each write overwrote the last.

These columns are the identity half of the fix (the name is derived separately, in
``domain/artifact_naming``). Every value is known to the RUNTIME at dispatch — the engine already
binds ``graph_id``, ``team_id`` and the member role onto each dispatch — so none of it is supplied
by the model, matching the rule ``graph_ingest`` already applies to ``graph_id``.

``producer_kind`` is what keeps the #522 refresh contract intact: a ``user-upload`` continues to key
its document node on the filename/path, so re-ingesting the same path still REPLACES.

Additive and non-destructive: every existing row gets NULL and keeps its current filename keying,
which is the behaviour it already had.

Revision ID: 0009_artifact_provenance
Revises: 0008_graph_model_credential
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_artifact_provenance"
down_revision: str | None = "0008_graph_model_credential"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("producer_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("team_run_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("member_role", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("execution_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("team_id", sa.String(length=255), nullable=True),
    )
    op.add_column("ingestion_jobs", sa.Column("ordinal", sa.Integer(), nullable=True))
    op.add_column(
        "ingestion_jobs",
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )
    # The two reads /v1/artifacts gains: "everything this run produced" and "everything this team
    # ever produced". Partial, because provenance is NULL on every pre-existing and uploaded row.
    op.create_index(
        "ix_ingestion_jobs_team_run_id",
        "ingestion_jobs",
        ["team_run_id"],
        postgresql_where=sa.text("team_run_id IS NOT NULL"),
    )
    op.create_index(
        "ix_ingestion_jobs_team_id",
        "ingestion_jobs",
        ["team_id"],
        postgresql_where=sa.text("team_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_team_id", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_team_run_id", table_name="ingestion_jobs")
    for column in (
        "content_hash",
        "ordinal",
        "team_id",
        "execution_id",
        "member_role",
        "team_run_id",
        "producer_kind",
    ):
        op.drop_column("ingestion_jobs", column)
