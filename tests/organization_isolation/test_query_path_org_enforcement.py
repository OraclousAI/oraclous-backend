"""Data-layer proof that A2's enforcement isolates organisations at runtime
(ORA-17 / A2, AC#4) — on the ORA-12 (0d) testcontainers harness.

RED until ``backend-implementer`` adds ``oraclous_substrate.query_scoping``.

Distinct from ORA-16/A1's ``organization_isolation`` tests, which proved the
*schema* carries ``organisation_id`` (org-scoped Neo4j indexes; Postgres org column
+ forced RLS policy + flags). Here we prove the *enforcement path A2 builds on top*
actually isolates at the data layer:

* **Neo4j** — ``org_scoped_cypher`` (reshape of ``_inject_graph_id_filter``) sources
  the filter from the bound org-context, so a read issued under org A's context
  never returns org B's nodes. Neo4j community has no RLS backstop, so this app-layer
  enforcement is the *primary* line of defence (ORA-17 brief addendum). Proven on the
  read path via the enforcement helper — not a hand-written ``WHERE``.
* **Postgres** — ``bind_organisation_guc`` (reshape of ``get_db()``) sets the RLS GUC
  from the org-context, activating A1's row-level-security policy so reads *and*
  writes are isolated, and a missing context fails closed (zero rows visible).

Write isolation (AC#4 "cannot ... write org B") is proven on Postgres via the RLS
WITH CHECK; the Neo4j half proves read isolation (the named ``_inject_graph_id_filter``
reshape is a read-path filter). Flagged for Tests Review: whether a Neo4j write-path
helper (stamping the context org onto ``CREATE``) is also in A2 scope, given Neo4j has
no WITH-CHECK equivalent.

Threats: T1-M1. ADR-006; ADR-004.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _ctx(org: uuid.UUID):
    from oraclous_governance.context import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=org,
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


# ── Neo4j: org_scoped_cypher isolates reads at the data layer ────────────────


def test_org_scoped_cypher_read_isolates_neo4j_nodes(neo4j_driver) -> None:
    from oraclous_governance.propagation import use_organisation_context
    from oraclous_substrate.query_scoping import org_scoped_cypher

    label = "__Entity__"
    marker = f"ora17-{uuid.uuid4()}"
    # The read template scopes via the helper, not a hand-written WHERE. If the
    # org filter were missing, each org's count would be 2 (both seeded nodes).
    base = f"MATCH (node:`{label}` {{_ora17: $marker}})\nRETURN count(node) AS c"
    try:
        for org in (ORG_A, ORG_B):
            neo4j_driver.execute_query(
                f"CREATE (:`{label}` {{organisation_id: $org, _ora17: $marker}})",
                org=str(org),
                marker=marker,
            )

        with use_organisation_context(_ctx(ORG_A)):
            query_a, params_a = org_scoped_cypher(base)
        records_a, _, _ = neo4j_driver.execute_query(query_a, marker=marker, **params_a)
        assert records_a[0]["c"] == 1  # org A sees only its own node, never org B's

        with use_organisation_context(_ctx(ORG_B)):
            query_b, params_b = org_scoped_cypher(base)
        records_b, _, _ = neo4j_driver.execute_query(query_b, marker=marker, **params_b)
        assert records_b[0]["c"] == 1  # org B sees only its own node, never org A's
    finally:
        neo4j_driver.execute_query("MATCH (n {_ora17: $marker}) DETACH DELETE n", marker=marker)


# ── Postgres: bind_organisation_guc activates RLS isolation at runtime ───────


def test_org_guc_isolates_postgres_reads_and_writes(postgres_dsn: str) -> None:
    import psycopg
    from oraclous_governance.propagation import use_organisation_context
    from oraclous_substrate.query_scoping import bind_organisation_guc
    from oraclous_substrate.schema import postgres as pg_schema

    # A trusted A1 tenant table (oraclous_substrate.schema.postgres.TENANT_TABLES);
    # named as a literal so the SQL is never dynamically constructed.
    assert "knowledge_graphs" in pg_schema.TENANT_TABLES
    insert_sql = (
        "INSERT INTO public.knowledge_graphs (organisation_id, user_id, name) VALUES (%s, %s, %s)"
    )
    select_sql = "SELECT name FROM public.knowledge_graphs"

    with psycopg.connect(postgres_dsn) as conn:
        pg_schema.apply(conn)
        conn.commit()

        # WRITE org A's row with the GUC bound to org A (RLS WITH CHECK admits it).
        with conn.transaction(), conn.cursor() as cur:
            with use_organisation_context(_ctx(ORG_A)):
                bind_organisation_guc(cur)
            cur.execute(insert_sql, (str(ORG_A), str(uuid.uuid4()), "org-a-graph"))

        # READ under org A's GUC: the row is visible.
        with conn.transaction(), conn.cursor() as cur:
            with use_organisation_context(_ctx(ORG_A)):
                bind_organisation_guc(cur)
            cur.execute(select_sql)
            a_rows = [r[0] for r in cur.fetchall()]
        assert "org-a-graph" in a_rows

        # READ under org B's GUC: org A's row is invisible (RLS USING filters it).
        with conn.transaction(), conn.cursor() as cur:
            with use_organisation_context(_ctx(ORG_B)):
                bind_organisation_guc(cur)
            cur.execute(select_sql)
            b_rows = [r[0] for r in cur.fetchall()]
        assert "org-a-graph" not in b_rows

        # WRITE isolation: with org B's GUC bound, inserting a row stamped for org A
        # violates the RLS WITH CHECK — org B cannot write into org A's scope.
        with pytest.raises(psycopg.Error):
            with conn.transaction(), conn.cursor() as cur:
                with use_organisation_context(_ctx(ORG_B)):
                    bind_organisation_guc(cur)
                cur.execute(insert_sql, (str(ORG_A), str(uuid.uuid4()), "smuggled"))

        # FAIL-CLOSED at the data layer: with NO org GUC bound, RLS exposes no rows
        # (absent scope denies, never defaults — AC#2 / T1-M1).
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.knowledge_graphs")
            assert cur.fetchone()[0] == 0
