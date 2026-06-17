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

import uuid

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


def enable_rls_on(
    conn,
    table: str,
    *,
    org_column: str = ORG_COLUMN,
    org_column_is_uuid: bool = True,
    extra_read_org_id: str | uuid.UUID | None = None,
) -> None:
    """Enable + FORCE row-level security and a single org-isolation policy on an
    **existing** ``public.<table>`` (ADR-030 §1). Idempotent.

    The generic, table-creating-free RLS applier the per-service migrations call:
    the ~28 org-scoped tables across the seven Postgres services already exist via
    each service's own migrations, so they need RLS *added* to existing tables, not
    re-created (unlike :func:`apply`, which is KGS's table-creating reshape).

    Issues, idempotently::

        ALTER TABLE public."<table>" ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public."<table>" FORCE  ROW LEVEL SECURITY;   -- binds the owner too
        DROP POLICY IF EXISTS "<table>_org_isolation" ON public."<table>";
        CREATE POLICY "<table>_org_isolation" ON public."<table>"
          USING      (<read_predicate>)
          WITH CHECK (<org_expr> = NULLIF(current_setting(<ORG_GUC>, true), '')::uuid);
          -- <ORG_GUC> = 'app.current_organisation_id'

    ``WITH CHECK`` is mandatory (not just ``USING``): without it a cross-org **write**
    is admitted (only reads are filtered). With it, an insert/update stamping another
    org's id raises SQLSTATE 42501 (``InsufficientPrivilege``). ``FORCE`` makes the
    policy bite the table owner role as well, so a non-``oraclous_app`` (owner) path
    is also constrained. The ``NULLIF(...,'')`` guard fails closed to zero rows when
    the GUC is unbound or has reverted to the empty string on a pooled connection
    (T1-M1) rather than erroring on the ``::uuid`` cast.

    ``org_column_is_uuid`` (default ``True``) controls the **column** side of the
    comparison. The GUC is always compared as ``uuid`` (the binding seam re-parses
    every bound org through :class:`uuid.UUID`, so only a canonical uuid literal
    reaches the policy). When the org column is itself ``uuid`` (credential-broker,
    KGS — Slice 0) the comparison is ``<col> = …::uuid`` unchanged. When it is a
    ``text``/``varchar`` column that *holds* a uuid string (auth-service stores
    ``organisation_id`` as ``String`` — Slice 1), pass ``False`` so the column is
    cast — ``<col>::uuid = …::uuid`` — because Postgres has no implicit ``text = uuid``
    operator and the policy would otherwise raise ``operator does not exist`` on
    every scan. The org values in such a column are canonical uuids in production
    (the org id is ``str(uuid.uuid4())``), so the per-row ``::uuid`` cast is total.

    ``extra_read_org_id`` (default ``None``) WIDENS THE READ SIDE ONLY to also admit
    rows owned by a second, fixed org — the capability-registry's platform/global tool
    catalogue (``PLATFORM_ORG_ID``): every tenant must *read* the shared built-in tools
    alongside its own (ADR-006 platform-catalogue case), but must never *write* them.
    When given, the ``USING`` clause becomes ``(<org_expr> = <guc> OR <org_expr> =
    '<extra>'::uuid)`` while ``WITH CHECK`` stays the strict caller-org equality — so a
    cross-org read of the platform org is admitted but a cross-org WRITE (stamping the
    platform org, or any other org) is still denied with 42501. The literal is re-parsed
    through :class:`uuid.UUID` here so only a canonical uuid reaches the DDL (it is a
    trusted module constant, never request input). ``None`` (the default) leaves the
    policy at the strict ``USING == WITH CHECK`` shape — exactly the prior behaviour, so
    every existing call site (broker / KGS / auth) is byte-for-byte unchanged.

    The table name and org column are trusted callers' constants (a per-service
    manifest / model-derived column), never request input — hence the narrow
    ``# noqa: S608`` on the policy DDL. Transaction control is the caller's.
    """
    policy = f"{table}{_POLICY_SUFFIX}"
    org_expr = org_column if org_column_is_uuid else f"{org_column}::uuid"
    strict_predicate = f"{org_expr} = NULLIF(current_setting('{ORG_GUC}', true), '')::uuid"
    if extra_read_org_id is not None:
        # re-parse through uuid.UUID: only a canonical uuid literal reaches the read DDL.
        extra = uuid.UUID(str(extra_read_org_id))
        read_predicate = f"{strict_predicate} OR {org_expr} = '{extra}'::uuid"
    else:
        read_predicate = strict_predicate
    with conn.cursor() as cur:
        cur.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
        cur.execute(f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY')
        cur.execute(f'DROP POLICY IF EXISTS "{policy}" ON public."{table}"')
        cur.execute(  # noqa: S608 — only trusted constants are interpolated
            f'CREATE POLICY "{policy}" ON public."{table}" '
            f"USING ({read_predicate}) WITH CHECK ({strict_predicate})"
        )


def apply(conn) -> None:
    """Create every tenant table org-scoped, with forced RLS and an org-GUC policy.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` + an idempotent
    :func:`enable_rls_on` per table keep a second run a no-op. Transaction control
    is the caller's (this does not commit). Composes :func:`enable_rls_on` so the
    RLS shape (ENABLE+FORCE, USING **and** WITH CHECK, NULLIF fail-closed) lives in
    exactly one place across KGS's reshape and the per-service realizations (ADR-030
    §1) — no behaviour change for KGS.
    """
    with conn.cursor() as cur:
        for table, columns in _TABLE_COLUMNS.items():
            cur.execute(  # noqa: S608 — table/column names are trusted module constants
                f'CREATE TABLE IF NOT EXISTS public."{table}" '
                f"({ORG_COLUMN} uuid NOT NULL, {columns})"
            )
    for table in _TABLE_COLUMNS:
        enable_rls_on(conn, table)
