"""enable Postgres RLS backstop on the credential-broker org-scoped tables (ADR-030 Slice 0)

Revision ID: 0004_enable_rls
Revises: 0003_org_data_keys
Create Date: 2026-06-17

Activates the row-level-security backstop (ADR-012 §2, realized by ADR-030) on all four org-scoped
broker tables — ``user_credentials``, ``webhook_secrets``, ``delegated_tokens``, ``org_data_keys``.
For each: ENABLE + FORCE row-level security and a single ``<table>_org_isolation`` policy whose
``USING`` **and** ``WITH CHECK`` are ``organisation_id = NULLIF(current_setting(
'app.current_organisation_id', true), '')::uuid`` — so a cross-org read is filtered AND a cross-org
write is denied (SQLSTATE 42501), and an unbound GUC fails closed to zero rows (T1-M1).

Composes the substrate ``enable_rls_on`` (the one place the RLS shape lives, ADR-030 §1) over a
pinned table list — RLS is *added to existing tables* (created by 0001–0003), never re-created.
Runs as the migration OWNER (``oraclous``): ENABLE/FORCE RLS and policy DDL require table ownership.
The runtime service connects as the NOSUPERUSER ``oraclous_app`` role (ADR-030 §3), under which the
policy bites. Idempotent: ``enable_rls_on`` drop-then-creates the policy and the toggles are no-ops
on a second run, so a redeploy is safe. ``downgrade`` disables RLS + drops the policies.

App-layer ``WHERE organisation_id = …`` filtering stays in the repositories — RLS is the *backstop*
(defense-in-depth), not a replacement (ADR-030).
"""

from __future__ import annotations

from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on

revision = "0004_enable_rls"
down_revision = "0003_org_data_keys"
branch_labels = None
depends_on = None

# The credential-broker's org-scoped tables (each carries a NOT NULL organisation_id). Pinned here
# rather than reflected so a new table cannot silently dodge RLS — the check_rls_coverage guardrail
# cross-checks this set against the realized-services manifest.
_RLS_TABLES = ("user_credentials", "webhook_secrets", "delegated_tokens", "org_data_keys")

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
