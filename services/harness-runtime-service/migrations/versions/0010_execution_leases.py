"""cross-replica cancel lease for in-flight harness executions (#1072)

Revision ID: 0010_execution_leases
Revises: 0009_fetched_urls
Create Date: #1072

``harness_execution_leases`` carries the cancel signal across Helm's ``replicas: 2`` (the service
has no Redis, so an in-process task registry can't be seen by the replica that didn't dispatch the
run): one row per in-flight execution, keyed by the globally unique ``execution_id`` the engine
mints per dispatch — a second ``create`` for the same id is a PK conflict the repository turns
into ``DuplicateExecutionId`` (the service maps it to 409). ``cancel_requested_at`` starts NULL and
is set once a caller org-scoped ``request_cancel``s it; the owning replica's watcher polls
``is_cancel_requested`` and cancels the loop task. The row is deleted (``release``) once the
terminal row is persisted — a crashed replica just leaves a stale row behind (harmless; cleanup is
a follow-up, per the #1072 design ruling).

Gets the SAME strict org-isolation policy shape as the other four harness tables (0006_enable_rls):
ENABLE + FORCE row-level security, ``USING`` == ``WITH CHECK`` == caller-org equality, an unbound
GUC failing closed to zero rows (T1-M1). No shared platform-catalogue case here either, so no
read-widening. Created straight from this migration (unlike 0006, which added RLS to four
already-existing tables) — ``enable_rls_on`` runs immediately after ``create_table``, in the same
idempotent shape 0006 uses, as the migration OWNER (``oraclous``, table-owner privilege required for
ENABLE/FORCE RLS + policy DDL).

No ``updated_at`` — the row is only ever created, flipped, read, or deleted outright, never patched
in place other than the one flag, so a second timestamp column would never be read.

The runtime role (``oraclous_app``) GRANT is provisioned separately by the idempotent
``core.bootstrap_rls_role`` one-shot (not this migration, matching 0001-0009): it now lists this
table in its own ``_RLS_TABLES`` alongside the other four.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on
from sqlalchemy.dialects import postgresql as pg

revision: str = "0010_execution_leases"
down_revision: str | None = "0009_fetched_urls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "harness_execution_leases"
_POLICY_SUFFIX = "_org_isolation"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("execution_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(f"ix_{_TABLE}_organisation_id", _TABLE, ["organisation_id"])

    # enable_rls_on speaks the DB-API cursor protocol; the Alembic bind exposes a raw psycopg
    # connection via .connection. Idempotent (drop-then-create policy + idempotent ENABLE/FORCE).
    # Table name passed as a literal (matching 0006's style) so the check_rls_coverage guardrail's
    # static scan credits this migration for the manifest entry.
    bind = op.get_bind()
    enable_rls_on(bind.connection, "harness_execution_leases")


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS "{_TABLE}{_POLICY_SUFFIX}" ON public."{_TABLE}"')
    op.execute(f'ALTER TABLE public."{_TABLE}" NO FORCE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE public."{_TABLE}" DISABLE ROW LEVEL SECURITY')
    op.drop_index(f"ix_{_TABLE}_organisation_id", table_name=_TABLE)
    op.drop_table(_TABLE)
