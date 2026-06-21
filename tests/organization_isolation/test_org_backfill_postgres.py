"""Idempotent organisation backfill of the legacy Postgres tenant tables (D1).

RED until ``backend-implementer`` adds ``oraclous_substrate.migrations.org_backfill``.

Reshape (lift-tag **Reshape**) of the legacy add-column+backfill alembic style in
``knowledge-graph-builder/alembic/versions/`` and the idempotent bootstrap in
``app/core/database.py`` (L94-118). The migration takes a *pre-A1* deployment —
tenant tables that exist **without** ``organisation_id`` (scoped only by
``graph_id`` / ``user_id``) — and brings every row up to the org-scoped + RLS shape
that ``oraclous_substrate.schema.postgres.apply`` declares for a fresh deployment,
seeding the well-known ``SEED_ORGANISATION_ID`` so a single-org deployment keeps
behaving as before (ADR-006, T1: a missed path is a cross-org read).

These are *data + schema* tests on the real Postgres harness: a legacy un-scoped
state is seeded, the migration runs, and the catalog + rows are introspected.
Per the product-planner boundary note (2026-05-29) the migration is *authored and
rehearsed* now; the production run is gated on A2/A3. Runtime RLS write/query
enforcement is A2, explicitly out of scope here — these assert the
backfill leaves no row unscoped and re-running is a no-op, plus a tested rollback.

Migration contract under test (to be implemented):
  ``backfill_postgres(conn, *, organisation_id=SEED_ORGANISATION_ID) -> None``
  ``rollback_postgres(conn) -> None``
Both operate on the caller's connection and do **not** commit (the caller controls
the transaction), mirroring ``schema.postgres.apply``.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

ROWS_PER_TABLE = 2

# Legacy (pre-A1) tenant tables: the target ``schema.postgres`` column sets *minus*
# organisation_id, with blob_cas keyed on the legacy (graph_id, sha256) composite PK.
# The migration's job is to add organisation_id as the outer scope across all of them.
_LEGACY_TABLE_DDL: dict[str, str] = {
    "knowledge_graphs": (
        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
        "user_id uuid NOT NULL, name text NOT NULL, "
        "status text NOT NULL DEFAULT 'active', "
        "created_at timestamptz NOT NULL DEFAULT now()"
    ),
    "ingestion_jobs": (
        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
        "graph_id uuid NOT NULL, status text NOT NULL DEFAULT 'pending', "
        "created_at timestamptz NOT NULL DEFAULT now()"
    ),
    "connectors": (
        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
        "graph_id uuid NOT NULL, user_id uuid NOT NULL, "
        "connector_type text NOT NULL, status text NOT NULL DEFAULT 'active', "
        "created_at timestamptz NOT NULL DEFAULT now()"
    ),
    "blob_cas": (
        "graph_id uuid NOT NULL, sha256 char(64) NOT NULL, mime_type text NOT NULL, "
        "size_bytes bigint NOT NULL, content bytea NOT NULL, "
        "created_at timestamptz NOT NULL DEFAULT now(), "
        "PRIMARY KEY (graph_id, sha256)"
    ),
}


def _seed_legacy_rows(cur) -> None:
    """Insert ROWS_PER_TABLE legacy rows per tenant table, none carrying an org."""
    g1, g2, u1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cur.execute(
        "INSERT INTO knowledge_graphs (user_id, name) VALUES (%s, %s), (%s, %s)",
        (u1, "graph-a", u1, "graph-b"),
    )
    cur.execute("INSERT INTO ingestion_jobs (graph_id) VALUES (%s), (%s)", (g1, g2))
    cur.execute(
        "INSERT INTO connectors (graph_id, user_id, connector_type) "
        "VALUES (%s, %s, %s), (%s, %s, %s)",
        (g1, u1, "gdrive", g2, u1, "slack"),
    )
    cur.execute(
        "INSERT INTO blob_cas (graph_id, sha256, mime_type, size_bytes, content) "
        "VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)",
        (g1, "a" * 64, "text/plain", 3, b"abc", g2, "b" * 64, "text/plain", 3, b"xyz"),
    )


def _drop_tenant_tables(conn, tables) -> None:
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f'DROP TABLE IF EXISTS public."{table}" CASCADE')
    conn.commit()


@pytest.fixture
def legacy_pg(postgres_dsn: str):
    """A real Postgres with the tenant tables in their *legacy*, un-org-scoped form.

    Drops any prior tenant tables (the harness Postgres is session-shared), recreates
    them without organisation_id, seeds rows, and tears them down afterwards so this
    suite neither sees nor leaves an org-scoped schema for its neighbours.
    """
    import psycopg
    from oraclous_substrate.schema import postgres as pg_schema

    tables = pg_schema.TENANT_TABLES
    # Guard: the legacy fixtures must cover the whole live registry, else the
    # migration would backfill a table this test never created.
    missing = set(tables) - set(_LEGACY_TABLE_DDL)
    assert not missing, f"legacy DDL missing for tenant tables: {sorted(missing)}"

    with psycopg.connect(postgres_dsn) as conn:
        _drop_tenant_tables(conn, tables)
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f'CREATE TABLE public."{table}" ({_LEGACY_TABLE_DDL[table]})')
            _seed_legacy_rows(cur)
        conn.commit()
        try:
            yield conn, pg_schema
        finally:
            _drop_tenant_tables(conn, tables)


def _backfill(conn) -> None:
    from oraclous_substrate.migrations import org_backfill

    org_backfill.backfill_postgres(conn)
    conn.commit()


def _org_column(pg_schema) -> str:
    return getattr(pg_schema, "ORG_COLUMN", "organisation_id")


def _count(cur, table: str, where: str = "") -> int:
    clause = f" WHERE {where}" if where else ""
    cur.execute(f'SELECT count(*) FROM public."{table}"{clause}')  # noqa: S608 - trusted constants
    return cur.fetchone()[0]


def _distinct_orgs(cur, table: str, org_col: str) -> list[str]:
    # table/column are trusted module constants, never request input (see _count).
    cur.execute(f'SELECT array_agg(DISTINCT {org_col}::text) FROM public."{table}"')  # noqa: S608
    return cur.fetchone()[0] or []


def test_backfill_adds_org_column_as_not_null_uuid(legacy_pg) -> None:
    conn, pg_schema = legacy_pg
    org_col = _org_column(pg_schema)
    _backfill(conn)
    for table in pg_schema.TENANT_TABLES:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
                (table, org_col),
            )
            row = cur.fetchone()
        assert row is not None, f"{table} still has no {org_col} after backfill"
        data_type, is_nullable = row
        assert data_type == "uuid", f"{table}.{org_col} is {data_type}, expected uuid"
        assert is_nullable == "NO", f"{table}.{org_col} must be NOT NULL after backfill"


def test_backfill_seeds_every_existing_row_to_the_seed_org(legacy_pg) -> None:
    """All pre-existing rows are scoped to SEED_ORGANISATION_ID, none lost or added."""
    conn, pg_schema = legacy_pg
    org_col = _org_column(pg_schema)
    from oraclous_substrate.organisation import SEED_ORGANISATION_ID

    _backfill(conn)
    for table in pg_schema.TENANT_TABLES:
        with conn.cursor() as cur:
            total = _count(cur, table)
            distinct_orgs = _distinct_orgs(cur, table, org_col)
        assert total == ROWS_PER_TABLE, f"{table} row count changed during backfill"
        assert distinct_orgs == [str(SEED_ORGANISATION_ID)], (
            f"{table} rows are not all scoped to the seed org: {distinct_orgs}"
        )


def test_backfill_leaves_no_row_unscoped(legacy_pg) -> None:
    """AC#2 / T1: after the migration no primitive is left without an organisation."""
    conn, pg_schema = legacy_pg
    org_col = _org_column(pg_schema)
    _backfill(conn)
    for table in pg_schema.TENANT_TABLES:
        with conn.cursor() as cur:
            unscoped = _count(cur, table, f"{org_col} IS NULL")
        assert unscoped == 0, f"{table} has {unscoped} row(s) with no organisation_id"


@pytest.mark.security
def test_backfill_enables_and_forces_rls(legacy_pg) -> None:
    """The migrated tables match the fresh-deploy shape: RLS enabled *and* forced."""
    conn, pg_schema = legacy_pg
    _backfill(conn)
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
        assert enabled, f"RLS not enabled on {table} after backfill"
        assert forced, f"RLS not forced on {table} (owner would bypass the policy)"


@pytest.mark.security
def test_backfill_creates_an_org_context_policy(legacy_pg) -> None:
    conn, pg_schema = legacy_pg
    org_col = _org_column(pg_schema)
    org_guc = pg_schema.ORG_GUC
    _backfill(conn)
    for table in pg_schema.TENANT_TABLES:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT qual FROM pg_policies WHERE schemaname = 'public' AND tablename = %s",
                (table,),
            )
            quals = [r[0] or "" for r in cur.fetchall()]
        assert quals, f"{table} has no RLS policy after backfill"
        assert any(org_col in q and org_guc in q for q in quals), (
            f"no policy on {table} references {org_col} and the org GUC {org_guc!r}; "
            f"found quals: {quals}"
        )


def test_backfill_is_idempotent(legacy_pg) -> None:
    """AC#1: re-running the backfill is a no-op — no new rows, no re-scoping, no extra policies."""
    conn, pg_schema = legacy_pg
    org_col = _org_column(pg_schema)
    from oraclous_substrate.migrations import org_backfill

    def snapshot() -> dict[str, tuple[int, int, int, bool, bool]]:
        state: dict[str, tuple[int, int, int, bool, bool]] = {}
        for table in pg_schema.TENANT_TABLES:
            with conn.cursor() as cur:
                rows = _count(cur, table)
                distinct_orgs = len(_distinct_orgs(cur, table, org_col))
                cur.execute(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE schemaname = 'public' AND tablename = %s",
                    (table,),
                )
                policies = cur.fetchone()[0]
                cur.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = %s AND relnamespace = 'public'::regnamespace",
                    (table,),
                )
                enabled, forced = cur.fetchone()
            state[table] = (rows, distinct_orgs, policies, enabled, forced)
        return state

    _backfill(conn)
    before = snapshot()
    org_backfill.backfill_postgres(conn)  # second run on already-migrated schema must not raise
    conn.commit()
    after = snapshot()
    assert after == before, f"backfill_postgres is not idempotent (before={before}, after={after})"


def test_rollback_reverts_scoping_without_losing_rows(legacy_pg) -> None:
    """AC#3: rollback removes the org column + policy and preserves the original rows."""
    conn, pg_schema = legacy_pg
    org_col = _org_column(pg_schema)
    from oraclous_substrate.migrations import org_backfill

    _backfill(conn)
    org_backfill.rollback_postgres(conn)
    conn.commit()

    for table in pg_schema.TENANT_TABLES:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
                (table, org_col),
            )
            org_col_present = cur.fetchone() is not None
            cur.execute(
                "SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = %s",
                (table,),
            )
            policies = cur.fetchone()[0]
            rows = _count(cur, table)
        assert not org_col_present, f"rollback left an {org_col} column on {table}"
        assert policies == 0, f"rollback left {policies} RLS policy(ies) on {table}"
        assert rows == ROWS_PER_TABLE, f"rollback lost rows on {table} ({rows} remain)"
