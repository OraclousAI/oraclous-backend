"""engine_provenance.context/input_hash/output_hash — the §3.7 extension point

Revision ID: 0029_provenance_context_hashes
Revises: 0028_engine_app_form
Create Date: #826 — solution-architect ruling (24 August 2026) + CTO ruling (11 September 2026)

Three additive, NULLABLE columns on ``engine_provenance``: ``context`` (structured per-call
detail — e.g. a team-run member's role — that used to be concatenated into ``resource``/``outcome``
as a string) and ``input_hash``/``output_hash`` (``sha256:<hex>`` content fingerprints; the raw
payload is never stored, CLAUDE.md §11). All three are nullable — a lifecycle event with no
per-call detail or input/output to attest carries NULL, never a defaulted ``{}``/``""``. The five
original columns are untouched.

No RLS change: ``engine_provenance`` is already covered (migration 0004); this is columns only on an
already-forced-RLS table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0029_provenance_context_hashes"
down_revision = "0028_engine_app_form"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("engine_provenance", sa.Column("context", pg.JSONB(), nullable=True))
    op.add_column("engine_provenance", sa.Column("input_hash", sa.String(128), nullable=True))
    op.add_column("engine_provenance", sa.Column("output_hash", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("engine_provenance", "output_hash")
    op.drop_column("engine_provenance", "input_hash")
    op.drop_column("engine_provenance", "context")
