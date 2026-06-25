"""delivery_state table + RLS (#515, E6 / O7 deliver-back clean-delta)

Revision ID: 0007_delivery_state
Revises: 0006_enable_rls
Create Date: 2026-06-25

The persisted half of the deliver-back clean-delta: the last-written per-file content hash for an
``(organisation_id, repo, ref, path)`` (so a recurring deliver writes only the diff, never a
clobber) plus the whole-delivery ``delivery_key`` (so an identical re-deliver dedupes to a NO_OP).
Org-scoped (``organisation_id`` NOT NULL — ADR-006) and STRICT RLS like the other registry tables
(ADR-030 §1): ``enable_rls_on`` is the one place the policy shape lives. ``UNIQUE(org, repo, ref,
path)`` keeps one row per file; the dedup rides on a scoped lookup (a per-row unique on
``delivery_key`` would wrongly reject the 2nd..Nth file of the same delivery, which share that key).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on
from sqlalchemy.dialects import postgresql as pg

revision = "0007_delivery_state"
down_revision = "0006_enable_rls"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")
_POLICY_SUFFIX = "_org_isolation"


def upgrade() -> None:
    op.create_table(
        "delivery_state",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("delivery_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.UniqueConstraint(
            "organisation_id", "repo", "ref", "path", name="uq_delivery_state_org_repo_ref_path"
        ),
    )
    op.create_index(
        "ix_delivery_state_org_repo_ref", "delivery_state", ["organisation_id", "repo", "ref"]
    )
    op.create_index(
        "ix_delivery_state_org_delivery_key", "delivery_state", ["organisation_id", "delivery_key"]
    )
    # STRICT RLS (ADR-030): the one substrate shape — ENABLE + FORCE + an org-isolation policy.
    enable_rls_on(op.get_bind().connection, "delivery_state")


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS "delivery_state{_POLICY_SUFFIX}" ON public."delivery_state"')
    op.execute('ALTER TABLE public."delivery_state" NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public."delivery_state" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_delivery_state_org_delivery_key", table_name="delivery_state")
    op.drop_index("ix_delivery_state_org_repo_ref", table_name="delivery_state")
    op.drop_table("delivery_state")
