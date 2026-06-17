"""enable Postgres RLS backstop on the knowledge-graph-service org-scoped tables (ADR-030 / #353)

Revision ID: 0007_enable_rls
Revises: 0006_graph_system_kind
Create Date: 2026-06-17

Activates the row-level-security backstop (ADR-012 §2, realized by ADR-030) on all four org-scoped
KGS tables — ``knowledge_graphs``, ``ingestion_jobs``, ``recipes``, ``entity_resolutions``. Per
table: ENABLE + FORCE row-level security and a single ``<table>_org_isolation`` policy whose
``USING`` **and** ``WITH CHECK`` are ``organisation_id =
NULLIF(current_setting('app.current_organisation_id', true), '')::uuid`` — so a cross-org read is
filtered AND a cross-org write is denied (SQLSTATE 42501), and an unbound GUC fails closed to zero
rows (T1-M1).

All four ``organisation_id`` columns are ``uuid`` (``UUID(as_uuid=True)`` in
``repositories/models.py``), so ``enable_rls_on`` is called with the default
``org_column_is_uuid=True`` — the column-side comparison is ``organisation_id = …::uuid`` (no
per-row cast). The policy is TABLE-LEVEL and PK-agnostic: ``recipes`` carries a composite PK
``(id, version, organisation_id)`` and ``entity_resolutions`` a
``(organisation_id, graph_id, candidate_id)`` unique key, but RLS keys solely on the
``organisation_id`` column the policy names, so the policy applies cleanly to both regardless of key
shape.

Composes the substrate ``enable_rls_on`` (the one place the RLS shape lives, ADR-030 §1) over a
pinned table list — RLS is *added to existing tables* (created by 0001–0006), never re-created. Runs
as the migration OWNER (``oraclous``): ENABLE/FORCE RLS and policy DDL require table ownership. The
runtime service + worker connect as the NOSUPERUSER ``oraclous_app`` role (ADR-030 §3), under which
the policy bites; the migrate + seed one-shots stay on the owner DSN (DDL + seed are owner
privileges, and the owner bypasses RLS so the unbound-GUC seed insert is admitted). Idempotent:
``enable_rls_on`` drop-then-creates the policy and the toggles are no-ops on a second run, so a
redeploy is safe. ``downgrade`` disables RLS + drops the policies.

App-layer ``WHERE organisation_id = …`` filtering stays in the repositories — RLS is the *backstop*
(defense-in-depth), not a replacement (ADR-030). Neo4j is out of scope (RLS is Postgres-only); KGS's
Neo4j graph data is isolated by the org-scoped-label / org-property write path, not this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on

revision: str = "0007_enable_rls"
down_revision: str | None = "0006_graph_system_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The KGS org-scoped tables (each carries a NOT NULL uuid organisation_id). Pinned here rather than
# reflected so a new table cannot silently dodge RLS — the check_rls_coverage guardrail cross-checks
# this set against the org-scoped models declared in repositories/models.py.
_RLS_TABLES = ("knowledge_graphs", "ingestion_jobs", "recipes", "entity_resolutions")

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
