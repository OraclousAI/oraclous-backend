"""engine_apps (+ RLS backstop with a WIDENED read)

Revision ID: 0026_engine_apps
Revises: 0025_team_run_mid_run_signals
Create Date: #932 — an app record, and the Oraclous-provided ones every organisation sees

``engine_apps`` is the Apps tab's persistence home: a team behind a short form, holding its OWN
frozen copy of that team's documents rather than a pointer to a draft (a draft's old versions are
not retained, and since ADR-050 D3 a saved draft keeps only ``manifest_ref``s to agents that are
editable in place). Org-scoped (ADR-006).

THE ONE DIFFERENCE FROM EVERY OTHER ENGINE TABLE. This one takes ``extra_read_org_id``, so its
policy reads::

    USING      (organisation_id = <guc> OR organisation_id = '<PLATFORM_ORG>')
    WITH CHECK (organisation_id = <guc>)

The read is widened; the write is NOT. That asymmetry is the entire mechanism by which an
Oraclous-provided app — seeded once into the platform organisation at startup — appears in every
tenant's Apps tab with nothing provisioned per tenant, while a tenant still cannot plant a row that
every other organisation would then see and run (the catalogue-poison case, 42501).

This is the SECOND use of that substrate hook; ``capability_descriptors`` in capability-registry
(migration 0006) is the first, for the built-in tool catalogue. It is the ADR-006
platform-catalogue case, not a new exception — and it is narrow: only the app ROW is shared. Every
run, input, result and artifact an app produces is stamped with the caller's organisation and read
strictly.

The literal is embedded here rather than imported from service settings, exactly as
capability-registry's 0006 does it: a migration must not depend on runtime configuration.

This table post-dates the RLS rollout (0004), so — like 0022 — it enables its own backstop in the
same migration (ADR-030). ``engine_apps`` is added to ``rls_coverage.yaml`` in the same change so
the coverage guardrail accounts for it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on
from sqlalchemy.dialects import postgresql as pg

revision = "0026_engine_apps"
down_revision = "0025_team_run_mid_run_signals"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")
_POLICY_SUFFIX = "_org_isolation"

#: The organisation that owns the Oraclous-provided apps. Same value capability-registry uses for
#: the built-in tool catalogue — one platform tenant, not one per feature.
_PLATFORM_ORG_ID = "00000000-0000-0000-0000-0000000000a0"


def upgrade() -> None:
    op.create_table(
        "engine_apps",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("slug", sa.String(length=128), nullable=True),
        sa.Column("manifest", pg.JSONB(), nullable=False),
        sa.Column("sub_harnesses", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("manifest_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("pinned_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_team_run_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("source_team_draft_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("source_draft_version", sa.Integer(), nullable=True),
        sa.Column(
            "credentials_mode", sa.String(length=16), nullable=False, server_default="caller"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index("ix_engine_apps_organisation_id", "engine_apps", ["organisation_id"])
    # A stable per-org handle for deep links. PARTIAL, so the many apps that never need a slug are
    # not forced to collide on NULL.
    op.create_index(
        "uq_engine_apps_org_slug",
        "engine_apps",
        ["organisation_id", "slug"],
        unique=True,
        postgresql_where=sa.text("slug IS NOT NULL"),
    )
    # The widened READ, strict WRITE (see the module docstring).
    enable_rls_on(op.get_bind().connection, "engine_apps", extra_read_org_id=_PLATFORM_ORG_ID)


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS "engine_apps{_POLICY_SUFFIX}" ON public."engine_apps"')
    op.execute('ALTER TABLE public."engine_apps" NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public."engine_apps" DISABLE ROW LEVEL SECURITY')
    op.drop_index("uq_engine_apps_org_slug", table_name="engine_apps")
    op.drop_index("ix_engine_apps_organisation_id", table_name="engine_apps")
    op.drop_table("engine_apps")
