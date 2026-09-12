"""registry_provenance table + RLS (#826, the 24 August + 11 September rulings)

Revision ID: 0008_registry_provenance
Revises: 0007_delivery_state
Create Date: 2026-09-11

The registry's own §3.7 audit sink behind the substrate ``ProvenanceCollector`` — every
``execute_sync`` dispatch (``capability.invoke``) and every pre-dispatch refusal
(``capability.refused``). Strictly org-scoped like ``executions`` (``organisation_id`` NOT NULL,
ADR-006) with STRICT RLS (ADR-030 §1): ``enable_rls_on`` is the one place the policy shape lives.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on
from sqlalchemy.dialects import postgresql as pg

revision = "0008_registry_provenance"
down_revision = "0007_delivery_state"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")
_POLICY_SUFFIX = "_org_isolation"


def upgrade() -> None:
    op.create_table(
        "registry_provenance",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("principal", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource", sa.String(length=512), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("context", pg.JSONB(), nullable=True),
        sa.Column("input_hash", sa.String(length=128), nullable=True),
        sa.Column("output_hash", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
    )
    op.create_index(
        "ix_registry_provenance_organisation_id", "registry_provenance", ["organisation_id"]
    )
    op.create_index("ix_registry_provenance_resource", "registry_provenance", ["resource"])
    # STRICT RLS (ADR-030): the one substrate shape — ENABLE + FORCE + an org-isolation policy.
    enable_rls_on(op.get_bind().connection, "registry_provenance")


def downgrade() -> None:
    op.execute(
        f'DROP POLICY IF EXISTS "registry_provenance{_POLICY_SUFFIX}" '
        'ON public."registry_provenance"'
    )
    op.execute('ALTER TABLE public."registry_provenance" NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public."registry_provenance" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_registry_provenance_resource", table_name="registry_provenance")
    op.drop_index("ix_registry_provenance_organisation_id", table_name="registry_provenance")
    op.drop_table("registry_provenance")
