"""engine_team_drafts (+ RLS backstop)

Revision ID: 0022_engine_team_drafts
Revises: 0021_team_run_revision_rounds
Create Date: #635 (C-1 — team drafts + compiler on-ramp ergonomics)

``engine_team_drafts`` is the compile → review → refine loop's persistence home (the re-scoped
C-1(b)): an editable, org-scoped OHM v1.1 Team Harness that has not (yet) been run. The console
loads a draft into the roster review, refines it with typed ops (each applied refine and each PUT
full-replace bumps ``version``), and finally GOes by POSTing the draft's ``manifest`` +
``sub_harnesses`` to ``/v1/engine/team-runs``. Org-scoped (ADR-006).

This table post-dates the RLS rollout (0004), so — like 0005/0009 — it enables its own
org-isolation backstop in the same migration (ADR-030): ENABLE + FORCE row level security + an
``engine_team_drafts_org_isolation`` policy via the substrate ``enable_rls_on`` (the single place
the RLS shape lives). The request path binds the org on the org-bound ``oraclous_app`` engine, so
the policy bites; the migration runs as the OWNER. ``engine_team_drafts`` is added to
``rls_coverage.yaml`` in the same change so the coverage guardrail accounts for it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on
from sqlalchemy.dialects import postgresql as pg

revision = "0022_engine_team_drafts"
down_revision = "0021_team_run_revision_rounds"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")
_POLICY_SUFFIX = "_org_isolation"


def upgrade() -> None:
    op.create_table(
        "engine_team_drafts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("manifest", pg.JSONB(), nullable=False),
        sa.Column("sub_harnesses", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW),
    )
    op.create_index(
        "ix_engine_team_drafts_organisation_id", "engine_team_drafts", ["organisation_id"]
    )
    # Enable the org-isolation RLS backstop (ADR-030) immediately — this table post-dates 0004.
    enable_rls_on(op.get_bind().connection, "engine_team_drafts")


def downgrade() -> None:
    op.execute(
        f'DROP POLICY IF EXISTS "engine_team_drafts{_POLICY_SUFFIX}" ON public."engine_team_drafts"'
    )
    op.execute('ALTER TABLE public."engine_team_drafts" NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public."engine_team_drafts" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_engine_team_drafts_organisation_id", table_name="engine_team_drafts")
    op.drop_table("engine_team_drafts")
