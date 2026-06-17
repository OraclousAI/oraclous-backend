"""enable Postgres RLS backstop on auth's always-org-bound tables (ADR-030 Slice 1)

Revision ID: 0007_enable_rls
Revises: 0006_audit
Create Date: 2026-06-17

Activates the row-level-security backstop (ADR-012 §2, realized by ADR-030) on auth-service's two
**always-org-bound** tables — ``agents`` and ``agent_credentials`` (each carries a NOT NULL
``organisation_id`` and is only ever accessed within a single org's context). For each: ENABLE +
FORCE row-level security and a single ``<table>_org_isolation`` policy whose ``USING`` **and**
``WITH CHECK`` are ``organisation_id::uuid = NULLIF(current_setting('app.current_organisation_id',
true), '')::uuid`` — so a cross-org read is filtered AND a cross-org write is denied (SQLSTATE
42501), and an unbound GUC fails closed to zero rows (T1-M1).

Auth-specific nuance vs Slice 0 (credential-broker): auth stores ``organisation_id`` as a ``String``
column, not ``uuid``. Postgres has no implicit ``text = uuid`` operator, so the policy would raise
``operator does not exist`` on every scan if the column were compared raw. We therefore pass
``org_column_is_uuid=False`` to :func:`enable_rls_on`, which casts the column side
(``organisation_id::uuid``). The org values stored there are canonical uuids in production (the org
id is ``str(uuid.uuid4())`` from org creation; the internal agent-create caller forwards a real org
uuid), so the per-row cast is total.

EXCLUDED — org-scoped but deliberately NOT RLS-enabled (documented in tools/lint/rls_coverage.yaml):
``org_members`` (login/resolve_active_org enumerate a user's memberships ACROSS orgs with no single
bound org — RLS would fail-close LOGIN), ``auth_audit_log`` (``organisation_id`` is NULLABLE — a
pre-org event has none), ``org_invitations`` / ``oauth_accounts`` / ``refresh_tokens`` (accessed in
pre-org / cross-org / token-lookup flows before an org is bound — RLS-deferred pending per-flow org
binding). ``users`` / ``organisations`` are identity/scope-root (no per-row org tenancy).

Composes the substrate ``enable_rls_on`` (the one place the RLS shape lives, ADR-030 §1) over a
pinned table list — RLS is *added to existing tables* (created by 0001), never re-created. Runs as
the migration OWNER (``oraclous``): ENABLE/FORCE RLS and policy DDL require table ownership. The
runtime credential store connects as the NOSUPERUSER ``oraclous_app`` role (ADR-030 §3), under which
the policy bites. Idempotent: ``enable_rls_on`` drop-then-creates the policy and the toggles are
no-ops on a second run, so a redeploy is safe. ``downgrade`` disables RLS + drops the policies.

App-layer ``WHERE organisation_id = …`` filtering stays in the credential store's admin surface —
RLS is the *backstop* (defense-in-depth), not a replacement (ADR-030).
"""

from __future__ import annotations

from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on

revision = "0007_enable_rls"
down_revision = "0006_audit"
branch_labels = None
depends_on = None

# Auth's always-org-bound tables (each carries a NOT NULL organisation_id, accessed only within an
# org context). Pinned here rather than reflected so a new table cannot silently dodge RLS — the
# check_rls_coverage guardrail cross-checks this set against the realized-services manifest.
_RLS_TABLES = ("agents", "agent_credentials")

_POLICY_SUFFIX = "_org_isolation"


def upgrade() -> None:
    bind = op.get_bind()
    # enable_rls_on speaks the DB-API cursor protocol; the Alembic bind exposes a raw psycopg
    # connection via .connection. org_column_is_uuid=False casts the String column to uuid for the
    # policy comparison (auth stores organisation_id as text — see module docstring). Each call is
    # idempotent (drop-then-create policy + idempotent ENABLE/FORCE), so re-running is a no-op.
    raw = bind.connection
    for table in _RLS_TABLES:
        enable_rls_on(raw, table, org_column_is_uuid=False)


def downgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(f'DROP POLICY IF EXISTS "{table}{_POLICY_SUFFIX}" ON public."{table}"')
        op.execute(f'ALTER TABLE public."{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
