"""enable Postgres RLS backstop on the application-gateway-service org-scoped tables (ADR-030/#353)

Revision ID: 0006_enable_rls
Revises: 0005_chat_message_rating
Create Date: 2026-06-17

Activates the row-level-security backstop (ADR-012 §2, realized by ADR-030) on all FIVE org-scoped
gateway tables — ``published_agents``, ``chat_threads``, ``chat_messages``, ``integration_keys``,
``webhook_subscriptions``. Per table: ENABLE + FORCE row-level security and a single
``<table>_org_isolation`` policy whose ``USING`` **and** ``WITH CHECK`` are ``organisation_id =
NULLIF(current_setting('app.current_organisation_id', true), '')::uuid`` — so a cross-org read is
filtered AND a cross-org write is denied (SQLSTATE 42501), and an unbound GUC fails closed to zero
rows (T1-M1).

All five ``organisation_id`` columns are ``uuid`` (``UUID(as_uuid=True)`` in ``models/``), so
``enable_rls_on`` is called with the default ``org_column_is_uuid=True`` — the column-side
comparison is ``organisation_id = …::uuid`` (no per-row cast).

The gateway has the same SPLIT the auth-service did (ADR-012 §1a + ADR-030 §3): three tables are
CLEAN (always reached with a bound org — ``published_agents`` / ``chat_threads`` / ``chat_messages``
on the org-bound ``oraclous_app`` engine, where RLS BITES), while ``integration_keys`` and
``webhook_subscriptions`` carry TWO pre-auth PRODUCER lookups that resolve BEFORE any org context:
``IntegrationKeyRepository.get_by_prefix`` (a UNIQUE prefix → the org it produces, the inbound
integration-key authz floor) and ``WebhookSubscriptionRepository.get_by_id`` (the opaque id IS the
inbound webhook's bearer-less credential). Those two reads MUST resolve cross-org, so they run on a
SEPARATE OWNER engine that bypasses RLS — else FORCE'd RLS fails them closed and breaks
integration-key auth + inbound webhooks (the HARD RULE). That is an engine/role split, NOT an RLS
exclusion: the tables themselves are fully RLS-enabled, and every ORG-BOUND op (key
create/list/rotate/revoke; subscription create/list/delete; the published-agent + chat CRUD) runs on
the org-bound engine under ``org_scope`` so reads filter and a cross-org write is denied. This
migration is engine-agnostic — it enables the policy uniformly; the two-engine carve is in
``core/rls`` + ``core/lifespan`` + the repositories + the two producer-calling services.

Composes the substrate ``enable_rls_on`` (the one place the RLS shape lives, ADR-030 §1) over a
pinned table list — RLS is *added to existing tables* (created by 0001–0005), never re-created. Runs
as the migration OWNER (``oraclous``): ENABLE/FORCE RLS and policy DDL require table ownership. The
runtime api connects as the NOSUPERUSER ``oraclous_app`` role (ADR-030 §3), under which the policy
bites; the gateway-migrate one-shot + the two pre-auth producer reads stay on the owner DSN (DDL is
an owner privilege, and the owner bypasses RLS so the producer reads are admitted). Idempotent:
``enable_rls_on`` drop-then-creates the policy and the toggles are no-ops on a second run, so a
redeploy is safe. ``downgrade`` disables RLS + drops the policies.

App-layer ``WHERE organisation_id = …`` filtering stays in the repositories — RLS is the *backstop*
(defense-in-depth), not a replacement (ADR-030).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on

revision: str = "0006_enable_rls"
down_revision: str | None = "0005_chat_message_rating"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The gateway's five org-scoped tables (each carries a NOT NULL uuid organisation_id). Pinned here
# rather than reflected so a new table cannot silently dodge RLS — the check_rls_coverage guardrail
# cross-checks this set against the org-scoped models under src.
_RLS_TABLES = (
    "published_agents",
    "chat_threads",
    "chat_messages",
    "integration_keys",
    "webhook_subscriptions",
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
