"""enable Postgres RLS backstop on the harness-runtime-service org-scoped tables (ADR-030 / #353)

Revision ID: 0006_enable_rls
Revises: 0005_execution_token_breakdown
Create Date: 2026-06-17

Activates the row-level-security backstop (ADR-012 §2, realized by ADR-030) on all four org-scoped
harness tables. All four get the plain STRICT org-isolation policy — the harness has no shared
platform-catalogue case, so no read-widening:

* ``harness_executions``, ``harness_checkpoints``, ``harness_assignments``, ``harness_provenance`` —
  STRICT. Per table: ENABLE + FORCE row-level security and a single ``<table>_org_isolation`` policy
  whose ``USING`` **and** ``WITH CHECK`` are ``organisation_id = NULLIF(current_setting(
  'app.current_organisation_id', true), '')::uuid`` — a cross-org read is filtered AND a cross-org
  write is denied (SQLSTATE 42501), and an unbound GUC fails closed to zero rows (T1-M1).

``harness_provenance`` is INSERT-ONLY at runtime (``PostgresProvenanceSink.write`` only ever inserts
audit rows); the same strict policy applies — the WITH CHECK is exercised on every provenance INSERT
(a cross-org provenance write raises 42501), and the USING side scopes the spend/audit reads.

All four ``organisation_id`` columns are ``uuid`` (``UUID(as_uuid=True)`` in the models), so
``enable_rls_on`` is called with the default ``org_column_is_uuid=True`` — the column-side compare
is ``organisation_id = …::uuid`` (no per-row cast). The policy is TABLE-LEVEL and PK-agnostic.

Composes the substrate ``enable_rls_on`` (the one place the RLS shape lives, ADR-030 §1) over a
pinned table list — RLS is *added to existing tables* (created by 0001–0005), never re-created. Runs
as the migration OWNER (``oraclous``): ENABLE/FORCE RLS and policy DDL require table ownership. The
runtime service connects as the NOSUPERUSER ``oraclous_app`` role (ADR-030 §3), under which the
policy bites; the harness-migrate one-shot stays on the owner DSN (DDL is an owner privilege, and
the owner bypasses RLS) and runs the shared ``provision_app_role`` bootstrap after this migration.
Idempotent: ``enable_rls_on`` drop-then-creates the policy and the toggles are no-ops on a second
run, so a redeploy is safe. ``downgrade`` disables RLS + drops the policies.

App-layer ``WHERE organisation_id = …`` filtering stays in the repositories, and each repository
binds the org via ``org_scope`` so the engine begin-guard sets the GUC before every query — RLS is
the *backstop* (defense-in-depth), not a replacement (ADR-030).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on

revision: str = "0006_enable_rls"
down_revision: str | None = "0005_execution_token_breakdown"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The four org-scoped harness tables (each carries a NOT NULL uuid organisation_id). Pinned here
# rather than reflected so a new table cannot silently dodge RLS — the check_rls_coverage guardrail
# cross-checks this set against the org-scoped models. All four get the plain strict policy.
_RLS_TABLES = (
    "harness_executions",
    "harness_checkpoints",
    "harness_assignments",
    "harness_provenance",
)

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
