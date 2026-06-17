"""enable Postgres RLS backstop on the capability-registry-service org-scoped tables (ADR-030/#353)

Revision ID: 0006_enable_rls
Revises: 0005_harness_graph_binding
Create Date: 2026-06-17

Activates the row-level-security backstop (ADR-012 §2, realized by ADR-030) on all four org-scoped
capability-registry tables. Three get the plain STRICT org-isolation policy;
``capability_descriptors`` gets a WIDENED-READ policy for the shared platform tool catalogue:

* ``tool_instances``, ``executions``, ``harness_graph_binding`` — STRICT. Per table: ENABLE + FORCE
  row-level security and a single ``<table>_org_isolation`` policy whose ``USING`` **and**
  ``WITH CHECK`` are ``organisation_id = NULLIF(current_setting('app.current_organisation_id',
  true), '')::uuid`` — a cross-org read is filtered AND a cross-org write is denied (SQLSTATE
  42501), and an unbound GUC fails closed to zero rows (T1-M1).

* ``capability_descriptors`` — WIDENED READ, STRICT WRITE. The repository widens *reads* to
  ``organisation_id IN (caller_org, PLATFORM_ORG_ID)`` so every tenant sees the shared built-in tool
  catalogue (owned by the platform org) alongside its own tools (ADR-006 platform-catalogue case).
  RLS must mirror that or it would fail-close the catalogue read for tenants. So its policy
  ``USING`` is ``organisation_id = <guc> OR organisation_id = '<PLATFORM_ORG_ID>'::uuid`` (admit the
  caller's org AND the platform org) while ``WITH CHECK`` stays the strict caller-org equality — a
  tenant can READ the platform built-ins but can never WRITE them (a cross-org INSERT/UPDATE,
  including stamping the platform org, still raises 42501). Applied via
  ``enable_rls_on(..., extra_read_org_id=...)`` so the widened shape lives in the one substrate
  place too (ADR-030 §1) — no hand-rolled policy DDL here.

All four ``organisation_id`` columns are ``uuid`` (``UUID(as_uuid=True)`` in the models), so
``enable_rls_on`` is called with the default ``org_column_is_uuid=True`` — the column-side compare
is ``organisation_id = …::uuid`` (no per-row cast). The policy is TABLE-LEVEL and PK-agnostic.

Composes the substrate ``enable_rls_on`` (the one place the RLS shape lives, ADR-030 §1) over a
pinned table list — RLS is *added to existing tables* (created by 0001–0005), never re-created. Runs
as the migration OWNER (``oraclous``): ENABLE/FORCE RLS and policy DDL require table ownership. The
runtime service connects as the NOSUPERUSER ``oraclous_app`` role (ADR-030 §3), under which the
policy bites; the migrate one-shot stays on the owner DSN (DDL is an owner privilege, and the owner
bypasses RLS). The startup plugin seed runs under the runtime role with the PLATFORM_ORG bound
(``org_scope``), so its INSERT of the built-in catalogue into the platform org satisfies the strict
WITH CHECK. Idempotent: ``enable_rls_on`` drop-then-creates the policy and the toggles are no-ops on
a second run, so a redeploy is safe. ``downgrade`` disables RLS + drops the policies.

App-layer ``WHERE organisation_id = …`` filtering (and the repository's widened read predicate)
stays in the repositories — RLS is the *backstop* (defense-in-depth), not a replacement (ADR-030).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from oraclous_substrate.schema.postgres import enable_rls_on

revision: str = "0006_enable_rls"
down_revision: str | None = "0005_harness_graph_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The org owning the built-in/platform tool catalogue. Must equal capability-registry config's
# PLATFORM_ORG_ID default and the CapabilityRepository widened-read org — embedded as a literal here
# so the RLS read-widening matches the repository's read-widening exactly (one canonical value).
_PLATFORM_ORG_ID = "00000000-0000-0000-0000-0000000000a0"

# The four org-scoped capability-registry tables (each carries a NOT NULL uuid organisation_id).
# Pinned here rather than reflected so a new table cannot silently dodge RLS — the
# check_rls_coverage guardrail cross-checks this set against the org-scoped models. The three STRICT
# tables get the plain policy; capability_descriptors (last) gets the widened-read policy below.
_STRICT_RLS_TABLES = ("tool_instances", "executions", "harness_graph_binding")

# capability_descriptors — RLS-enabled with the widened-read policy. Spelled as a string literal so
# the check_rls_coverage AST guardrail credits it as an enable_rls_on'd table (it collects literal
# table args / string-list literals from a migration that calls enable_rls_on).
_WIDENED_READ_TABLE = "capability_descriptors"

# Every RLS-enabled table (strict + widened) as a single literal tuple, for downgrade + coverage.
_ALL_RLS_TABLES = (
    "tool_instances",
    "executions",
    "harness_graph_binding",
    "capability_descriptors",
)

_POLICY_SUFFIX = "_org_isolation"


def upgrade() -> None:
    bind = op.get_bind()
    # enable_rls_on speaks the DB-API cursor protocol; the Alembic bind exposes a raw psycopg
    # connection via .connection. Each call is idempotent (drop-then-create policy + idempotent
    # ENABLE/FORCE), so re-running this migration is a no-op.
    raw = bind.connection
    for table in _STRICT_RLS_TABLES:
        enable_rls_on(raw, table)
    # capability_descriptors: reads widened to the platform org (shared built-in catalogue), writes
    # strict to the caller org (ADR-006 platform-catalogue case).
    enable_rls_on(raw, _WIDENED_READ_TABLE, extra_read_org_id=_PLATFORM_ORG_ID)


def downgrade() -> None:
    for table in _ALL_RLS_TABLES:
        op.execute(f'DROP POLICY IF EXISTS "{table}{_POLICY_SUFFIX}" ON public."{table}"')
        op.execute(f'ALTER TABLE public."{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
