"""ingestion_jobs.source — carry the connector's SourceRef to the worker (#742 / §CITE rev3)

A citation is minted server-side at the tool-execution boundary, but the ingest write happens in a
Celery worker: a separate process with no access to the request body. The job row is the only
channel across that boundary, so the connector-supplied `SourceRef` is stored verbatim alongside
the content it describes.

Nullable, with no backfill. Existing rows stay NULL, which is the Contract's defined meaning for a
record with no source identity — synthesising one for a document nobody captured a source for is
exactly the forged provenance the whole Contract exists to reject.

Revision ID: 0008_ingestion_job_source
Revises: 0007_enable_rls
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_ingestion_job_source"
down_revision: str | None = "0007_enable_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ingestion_jobs", sa.Column("source", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "source")
