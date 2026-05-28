"""One-time organisation backfill across the substrate stores (ORA-24 / D1).

Brings a pre-A1 deployment — substrate data scoped only by ``graph_id`` / ``user_id``
— up to the organisation-scoped shape A1 (ORA-16) declares, seeding the well-known
``SEED_ORGANISATION_ID`` so a single-organisation deployment keeps behaving as before
(ADR-006; T1: a primitive a query can reach without an organisation is a cross-org
read). Reshape of the legacy ``knowledge-graph-builder`` alembic add-column+backfill
migrations, the idempotent Neo4j setup scripts, and the query-cache key format.

Every store's entry point is idempotent (safe to re-run) and paired with a rollback.
The Postgres and Neo4j entry points operate on the caller's connection/driver and
leave transaction control to the caller, mirroring ``oraclous_substrate.schema.*.apply``.

Identifiers interpolated into the Postgres statements below are trusted module
constants (the ``schema.postgres`` table-name registry + ``ORG_COLUMN``) or
catalog-sourced policy names, never request input — hence the narrow ``# noqa: S608``.
The organisation value is always passed as a bound parameter.
"""

from __future__ import annotations

import uuid

from oraclous_substrate.organisation import SEED_ORGANISATION_ID
from oraclous_substrate.schema import neo4j as neo4j_schema
from oraclous_substrate.schema import postgres as pg_schema

_CACHE_PREFIX = "qcache"
# Legacy key shape ``qcache:{graph_id}:{sha256}`` has exactly three colon-separated
# segments; the A1 key ``qcache:{organisation_id}:{graph_id}:{digest}`` has four.
_LEGACY_CACHE_SEGMENTS = 3


# --- Postgres ---------------------------------------------------------------


def backfill_postgres(conn, *, organisation_id: uuid.UUID = SEED_ORGANISATION_ID) -> None:
    """Add + backfill ``organisation_id`` on every tenant table, then apply the org schema.

    For each ``schema.postgres.TENANT_TABLES`` table: add the column if missing, seed
    every un-scoped row to ``organisation_id``, mark it NOT NULL, then re-apply the
    canonical schema so the migrated table carries the same forced RLS + isolation
    policy a fresh deployment gets. Idempotent — ``ADD COLUMN IF NOT EXISTS``, an
    UPDATE that only touches NULLs, an already-satisfied ``SET NOT NULL`` and the
    idempotent ``apply`` make a second run a no-op. Does not commit.
    """
    org = str(organisation_id)
    org_col = pg_schema.ORG_COLUMN
    with conn.cursor() as cur:
        for table in pg_schema.TENANT_TABLES:
            cur.execute(f'ALTER TABLE public."{table}" ADD COLUMN IF NOT EXISTS {org_col} uuid')
            cur.execute(
                f'UPDATE public."{table}" SET {org_col} = %s WHERE {org_col} IS NULL',  # noqa: S608
                (org,),
            )
            cur.execute(f'ALTER TABLE public."{table}" ALTER COLUMN {org_col} SET NOT NULL')
    # Reuse the canonical org-scoped schema declaration for the forced-RLS + policy
    # shape (CREATE TABLE IF NOT EXISTS no-ops on the now-migrated tables).
    pg_schema.apply(conn)


def rollback_postgres(conn) -> None:
    """Revert the Postgres backfill, preserving the rows.

    Drops each tenant table's RLS policies, disables/unforces RLS, and drops the
    ``organisation_id`` column. Only the org scoping is removed — the original rows
    remain. Idempotent via ``IF EXISTS`` guards. Does not commit.
    """
    org_col = pg_schema.ORG_COLUMN
    with conn.cursor() as cur:
        for table in pg_schema.TENANT_TABLES:
            cur.execute(
                "SELECT policyname FROM pg_policies WHERE schemaname = 'public' AND tablename = %s",
                (table,),
            )
            for (policyname,) in cur.fetchall():
                cur.execute(f'DROP POLICY IF EXISTS "{policyname}" ON public."{table}"')
            cur.execute(f'ALTER TABLE public."{table}" NO FORCE ROW LEVEL SECURITY')
            cur.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
            cur.execute(f'ALTER TABLE public."{table}" DROP COLUMN IF EXISTS {org_col}')


# --- Neo4j ------------------------------------------------------------------


def backfill_neo4j(driver, *, organisation_id: uuid.UUID = SEED_ORGANISATION_ID) -> None:
    """Stamp the seed organisation onto every un-scoped org-scoped node + relationship.

    Sets ``organisation_id`` (as a string, matching the A1 cache/data convention) on
    every node of ``schema.neo4j.ORG_SCOPED_LABELS`` and every relationship of
    ``ORG_SCOPED_RELATIONSHIP_TYPES`` that lacks one, then (re-)applies the org-scoped
    indexes. Idempotent — the ``IS NULL`` guard makes a second run a no-op.
    """
    org = str(organisation_id)
    prop = neo4j_schema.ORG_PROPERTY
    for label in neo4j_schema.ORG_SCOPED_LABELS:
        driver.execute_query(
            f"MATCH (n:`{label}`) WHERE n.{prop} IS NULL SET n.{prop} = $org", org=org
        )
    for rel_type in neo4j_schema.ORG_SCOPED_RELATIONSHIP_TYPES:
        driver.execute_query(
            f"MATCH ()-[r:`{rel_type}`]->() WHERE r.{prop} IS NULL SET r.{prop} = $org",
            org=org,
        )
    neo4j_schema.apply(driver)


def rollback_neo4j(driver) -> None:
    """Revert the Neo4j backfill: remove ``organisation_id`` from the org-scoped graph."""
    prop = neo4j_schema.ORG_PROPERTY
    for label in neo4j_schema.ORG_SCOPED_LABELS:
        driver.execute_query(f"MATCH (n:`{label}`) WHERE n.{prop} IS NOT NULL REMOVE n.{prop}")
    for rel_type in neo4j_schema.ORG_SCOPED_RELATIONSHIP_TYPES:
        driver.execute_query(
            f"MATCH ()-[r:`{rel_type}`]->() WHERE r.{prop} IS NOT NULL REMOVE r.{prop}"
        )


# --- Redis ------------------------------------------------------------------


def migrate_redis_cache(redis_client) -> None:
    """Cold-start the query cache: drop legacy un-organisation-scoped ``qcache`` keys.

    A legacy entry ``qcache:{graph_id}:{sha256}`` cannot be backfilled in place (its
    key carries only the query hash, so the A1 org-then-graph key cannot be
    recomputed), so the only way to guarantee no stale cross-prefix read is to remove
    it and let the cache repopulate under the org-scoped key. Scoped to the cache
    namespace — non-``qcache`` keys and already-org-scoped (4-segment) keys are left
    untouched. Idempotent.
    """
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(cursor=cursor, match=f"{_CACHE_PREFIX}:*", count=100)
        legacy = [k for k in keys if len(k.split(":")) == _LEGACY_CACHE_SEGMENTS]
        if legacy:
            redis_client.delete(*legacy)
        if cursor == 0:
            return
