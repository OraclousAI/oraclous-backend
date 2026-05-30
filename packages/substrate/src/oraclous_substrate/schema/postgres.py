"""Organisation-scoped Postgres substrate schema + RLS (ORA-16 / A1, AC#2).

Reshape of the knowledge-graph tenant tables in
``knowledge-graph-builder/app/models/graph.py`` (scoped by ``graph_id`` /
``user_id``): ``organisation_id`` becomes the outermost tenancy scope, and
row-level security is the defense-in-depth backstop behind the write path
(ADR-006). RLS is ``FORCE``-d so it also constrains the table owner role.

``apply(conn)`` takes any DB-API/psycopg connection and is idempotent — re-running
it neither raises nor duplicates policies. The org column, RLS toggles and the
isolation policy are applied uniformly so no tenant table can be enrolled without
them. The org GUC (``ORG_GUC``) is set per-connection by the write path (A2/ORA-17),
which is out of scope here.

Identifiers interpolated into DDL are trusted module constants (the table-name
registry below and the ``ORG_*`` constants), never request input — hence the
narrow ``# noqa: S608`` on the DDL statements.
"""

from __future__ import annotations

ORG_COLUMN = "organisation_id"
ORG_GUC = "app.current_organisation_id"
_POLICY_SUFFIX = "_org_isolation"

# Each entry is the table's column DDL *excluding* organisation_id, which apply()
# prepends uniformly. Reshaped to add organisation_id as the outer scope above the
# legacy graph_id / user_id; column sets are trimmed to the tenancy-relevant core
# (later releases own the full per-table schema).
_TABLE_COLUMNS: dict[str, str] = {
    "knowledge_graphs": (
        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
        "user_id uuid NOT NULL, "
        "name text NOT NULL, "
        "status text NOT NULL DEFAULT 'active', "
        "created_at timestamptz NOT NULL DEFAULT now()"
    ),
    "ingestion_jobs": (
        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
        "graph_id uuid NOT NULL, "
        "status text NOT NULL DEFAULT 'pending', "
        "created_at timestamptz NOT NULL DEFAULT now()"
    ),
    "connectors": (
        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
        "graph_id uuid NOT NULL, "
        "user_id uuid NOT NULL, "
        "connector_type text NOT NULL, "
        "status text NOT NULL DEFAULT 'active', "
        "created_at timestamptz NOT NULL DEFAULT now()"
    ),
    "blob_cas": (
        "graph_id uuid NOT NULL, "
        "sha256 char(64) NOT NULL, "
        "mime_type text NOT NULL, "
        "size_bytes bigint NOT NULL, "
        "content bytea NOT NULL, "
        "created_at timestamptz NOT NULL DEFAULT now(), "
        f"PRIMARY KEY ({ORG_COLUMN}, graph_id, sha256)"
    ),
}

TENANT_TABLES: tuple[str, ...] = tuple(_TABLE_COLUMNS)


def apply(conn) -> None:
    """Create every tenant table org-scoped, with forced RLS and an org-GUC policy.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` + idempotent RLS toggles + a
    drop-then-create of the single isolation policy keep a second run a no-op.
    Transaction control is the caller's (this does not commit).
    """
    with conn.cursor() as cur:
        for table, columns in _TABLE_COLUMNS.items():
            policy = f"{table}{_POLICY_SUFFIX}"
            cur.execute(  # noqa: S608 — table/column names are trusted module constants
                f'CREATE TABLE IF NOT EXISTS public."{table}" '
                f"({ORG_COLUMN} uuid NOT NULL, {columns})"
            )
            cur.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
            cur.execute(f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY')
            cur.execute(f'DROP POLICY IF EXISTS "{policy}" ON public."{table}"')
            cur.execute(  # noqa: S608 — only trusted constants are interpolated
                f'CREATE POLICY "{policy}" ON public."{table}" '
                # NULLIF guards the ``::uuid`` cast against the empty-string GUC
                # that a custom (period-named) parameter reverts to once it has
                # been SET LOCAL in a prior transaction on a pooled connection.
                # Without it, an unbound scope errors with InvalidTextRepresentation
                # instead of cleanly denying — same end-state (no data leaks) but
                # fragile; with NULLIF the GUC's '' and unset both fail-closed to
                # zero rows (T1-M1; A2/ORA-17 integration test fail-closed half).
                f"USING ({ORG_COLUMN} = NULLIF(current_setting('{ORG_GUC}', true), '')::uuid)"
            )
