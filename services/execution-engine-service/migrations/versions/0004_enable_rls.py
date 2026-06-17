"""enable Postgres RLS backstop on the execution-engine-service org-scoped tables (ADR-030 / #353)

Revision ID: 0004_enable_rls
Revises: 0003_engine_roundtables
Create Date: 2026-06-17

Activates the row-level-security backstop (ADR-012 §2, realized by ADR-030) on all four org-scoped
execution-engine tables — ``engine_jobs``, ``engine_schedules``, ``engine_roundtables``,
``engine_provenance``. Per table: ENABLE + FORCE row-level security and a single
``<table>_org_isolation`` policy whose ``USING`` **and** ``WITH CHECK`` are ``organisation_id =
NULLIF(current_setting('app.current_organisation_id', true), '')::uuid`` — so a cross-org read is
filtered AND a cross-org write is denied (SQLSTATE 42501), and an unbound GUC fails closed to zero
rows (T1-M1).

All four ``organisation_id`` columns are ``uuid`` (``UUID(as_uuid=True)`` in ``models/``), so
``enable_rls_on`` is called with the default ``org_column_is_uuid=True`` — the column-side
comparison is ``organisation_id = …::uuid`` (no per-row cast).

The execution-engine has a SPLIT the single-engine services (KGS, credential-broker) did not: its
request/driver path + the org-bound Celery task execution connect as the NOSUPERUSER
``oraclous_app`` role (the org-bound engine — RLS BITES), but three cross-org MAINTENANCE sweeps
(the reaper ``list_stale_running`` + Beat ``list_enabled_cron``) read ACROSS orgs on the OWNER
engine (which bypasses RLS — else FORCE'd RLS fails the cross-org read closed). The per-row settle
AFTER
a sweep goes back through the org-bound engine with the row's own org bound (``org_scope``), so a
cross-org write is still denied. This migration is engine-agnostic — it enables the policy; the
two-engine
carve is in ``core/rls`` + ``repositories/maintenance_repository`` + the service sweeps.

Composes the substrate ``enable_rls_on`` (the one place the RLS shape lives, ADR-030 §1) over a
pinned table list — RLS is *added to existing tables* (created by 0001–0003), never re-created. Runs
as the migration OWNER (``oraclous``): ENABLE/FORCE RLS and policy DDL require table ownership. The
runtime api + the org-bound worker path connect as the NOSUPERUSER ``oraclous_app`` role (ADR-030
§3), under which the policy bites; the migrate one-shot + the maintenance/reaper/beat read engine
stay on the owner DSN (DDL is an owner privilege, and the owner bypasses RLS so the cross-org sweep
reads are admitted). Idempotent: ``enable_rls_on`` drop-then-creates the policy and the toggles are
no-ops on a second run, so a redeploy is safe. ``downgrade`` disables RLS + drops the policies.

App-layer ``WHERE organisation_id = …`` filtering stays in the repositories — RLS is the *backstop*
(defense-in-depth), not a replacement (ADR-030).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on

revision: str = "0004_enable_rls"
down_revision: str | None = "0003_engine_roundtables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The execution-engine's four org-scoped tables (each carries a NOT NULL uuid organisation_id).
# Pinned here rather than reflected so a new table cannot silently dodge RLS — the
# check_rls_coverage guardrail cross-checks this set against the org-scoped models under src.
_RLS_TABLES = ("engine_jobs", "engine_schedules", "engine_roundtables", "engine_provenance")

_POLICY_SUFFIX = "_org_isolation"


def upgrade() -> None:
    bind = op.get_bind()
    # enable_rls_on speaks the DB-API cursor protocol; the Alembic bind exposes a raw psycopg
    # connection via .connection. Each call is idempotent (drop-then-create policy + idempotent
    # ENABLE/FORCE), so re-running this migration is a no-op.
    raw = bind.connection
    for table in _RLS_TABLES:
        enable_rls_on(raw, table)


def downgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(f'DROP POLICY IF EXISTS "{table}{_POLICY_SUFFIX}" ON public."{table}"')
        op.execute(f'ALTER TABLE public."{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
