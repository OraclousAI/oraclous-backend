"""Every substrate Postgres tenant table is organisation-scoped with RLS (ORA-16 / A1, AC#2).

RED until `backend-implementer` adds `oraclous_substrate.schema.postgres`.

Reshape (lift-tag **Reshape**) of the legacy tenant tables in
``knowledge-graph-builder/app/models/graph.py`` and ``.../chat.py`` (scoped by
``graph_id`` / ``user_id``) to carry ``organisation_id`` as the outer tenancy
unit, with row-level security as the defense-in-depth backstop (ADR-006, T1).

These are *schema* tests on the real Postgres harness (AC#6): they introspect
the catalog after the substrate applies its schema. Row-level write/query
enforcement is A2 (ORA-17), explicitly out of scope here.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]


@pytest.fixture(scope="module")
def applied_schema(postgres_dsn: str):
    """Apply the substrate Postgres schema once, then expose a connection + metadata."""
    import psycopg
    from oraclous_substrate.schema import postgres as pg_schema

    with psycopg.connect(postgres_dsn) as conn:
        pg_schema.apply(conn)
        conn.commit()
        yield conn, pg_schema


def test_substrate_declares_tenant_tables(applied_schema) -> None:
    _conn, pg_schema = applied_schema
    assert len(pg_schema.TENANT_TABLES) > 0


def test_every_tenant_table_has_organisation_id_not_null_uuid(applied_schema) -> None:
    conn, pg_schema = applied_schema
    org_col = getattr(pg_schema, "ORG_COLUMN", "organisation_id")
    for table in pg_schema.TENANT_TABLES:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
                (table, org_col),
            )
            row = cur.fetchone()
        assert row is not None, f"{table} is missing an {org_col} column"
        data_type, is_nullable = row
        assert data_type == "uuid", f"{table}.{org_col} is {data_type}, expected uuid"
        assert is_nullable == "NO", f"{table}.{org_col} must be NOT NULL"


def test_every_tenant_table_enables_and_forces_rls(applied_schema) -> None:
    """RLS must be enabled *and* forced so it also applies to the table owner role."""
    conn, pg_schema = applied_schema
    for table in pg_schema.TENANT_TABLES:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = %s AND relnamespace = 'public'::regnamespace",
                (table,),
            )
            row = cur.fetchone()
        assert row is not None, f"{table} not found in pg_class"
        enabled, forced = row
        assert enabled, f"RLS not enabled on {table}"
        assert forced, f"RLS not forced on {table} (owner would bypass the policy)"


def test_every_tenant_table_has_a_policy_parameterised_on_the_org_context(applied_schema) -> None:
    conn, pg_schema = applied_schema
    org_col = getattr(pg_schema, "ORG_COLUMN", "organisation_id")
    org_guc = pg_schema.ORG_GUC
    for table in pg_schema.TENANT_TABLES:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT qual FROM pg_policies WHERE schemaname = 'public' AND tablename = %s",
                (table,),
            )
            quals = [r[0] or "" for r in cur.fetchall()]
        assert quals, f"{table} has no RLS policy"
        assert any(org_col in q and org_guc in q for q in quals), (
            f"no policy on {table} references {org_col} and the org GUC {org_guc!r}; "
            f"found quals: {quals}"
        )


def test_apply_is_idempotent(applied_schema) -> None:
    """Re-running apply() on an already-applied schema is safe (a real redeploy path).

    Per-table the RLS policy count and the relrowsecurity/relforcerowsecurity flags
    must be unchanged after a second apply, and the second apply must not raise. This
    guards the registry-drift risk in TENANT_TABLES (a re-apply must not duplicate
    policies or silently relax forced RLS).
    """
    conn, pg_schema = applied_schema

    def snapshot() -> dict[str, tuple[int, bool, bool]]:
        state: dict[str, tuple[int, bool, bool]] = {}
        for table in pg_schema.TENANT_TABLES:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE schemaname = 'public' AND tablename = %s",
                    (table,),
                )
                policy_count = cur.fetchone()[0]
                cur.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = %s AND relnamespace = 'public'::regnamespace",
                    (table,),
                )
                enabled, forced = cur.fetchone()
            state[table] = (policy_count, enabled, forced)
        return state

    before = snapshot()
    pg_schema.apply(conn)  # second apply on the already-applied schema must not raise
    conn.commit()
    after = snapshot()

    assert after == before, (
        "apply() is not idempotent: re-applying changed per-table policy counts or RLS "
        f"flags (before={before}, after={after})"
    )
