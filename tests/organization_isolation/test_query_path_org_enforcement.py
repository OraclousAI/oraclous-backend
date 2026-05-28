"""Data-layer proof that A2's enforcement isolates organisations at runtime
(ORA-17 / A2, AC#4) — on the ORA-12 (0d) testcontainers harness.

RED until ``backend-implementer`` adds ``oraclous_substrate.access``.

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

Write isolation (AC#4 "cannot ... write org B") is proven on both stores: Postgres
via the RLS WITH CHECK, and Neo4j via ``scoped_write_node`` (ADR-012), which stamps
the bound-context organisation onto ``CREATE`` and ignores any caller-supplied
``organisation_id``. Per the Tests Review ruling (solution-architect + security-architect,
ORA-17 escalation), the Neo4j write helper IS in A2 scope and the proof is required
here: Neo4j community has no WITH-CHECK/RLS backstop, so the app-layer write
enforcement is the primary control and must be proven, not deferred.

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
    from oraclous_substrate.access import org_scoped_cypher

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


# ── Neo4j: scoped_write_node stamps the context org on writes (no RLS backstop) ──


def test_scoped_write_node_stamps_context_org_and_ignores_body(neo4j_driver) -> None:
    """AC#4 write half (Tests Review ruling): the Neo4j write primitive stamps
    ``organisation_id`` from the bound context onto the created node and ignores a
    caller-supplied ``organisation_id`` in the body — so org A writing a node tagged
    for org B lands as org A's, and org B cannot see it. Neo4j community has no
    WITH-CHECK/RLS backstop, so this app-layer write control is the primary line of
    defence and is proven directly (T1-M1; ADR-012 ``scoped_write_node``).
    """
    from oraclous_governance.propagation import use_organisation_context
    from oraclous_substrate.access import org_scoped_cypher, scoped_write_node

    label = "__Entity__"
    marker = f"ora17w-{uuid.uuid4()}"
    count_base = f"MATCH (node:`{label}` {{_ora17w: $marker}})\nRETURN count(node) AS c"
    try:
        # org A writes a node, smuggling org B's id into the property body.
        with use_organisation_context(_ctx(ORG_A)):
            scoped_write_node(
                neo4j_driver,
                label=label,
                properties={"_ora17w": marker, "organisation_id": str(ORG_B)},
            )

        # Direct, unscoped read of the stored property: the stamp is the bound
        # context's org (A), never the body's (B) — the body value is ignored.
        stamped, _, _ = neo4j_driver.execute_query(
            f"MATCH (node:`{label}` {{_ora17w: $marker}})\nRETURN node.organisation_id AS org",
            marker=marker,
        )
        assert [r["org"] for r in stamped] == [str(ORG_A)]

        # org B cannot see org A's write (the smuggled-for-B node landed as org A's).
        with use_organisation_context(_ctx(ORG_B)):
            query_b, params_b = org_scoped_cypher(count_base)
        records_b, _, _ = neo4j_driver.execute_query(query_b, marker=marker, **params_b)
        assert records_b[0]["c"] == 0

        # org A sees exactly its own write.
        with use_organisation_context(_ctx(ORG_A)):
            query_a, params_a = org_scoped_cypher(count_base)
        records_a, _, _ = neo4j_driver.execute_query(query_a, marker=marker, **params_a)
        assert records_a[0]["c"] == 1
    finally:
        neo4j_driver.execute_query("MATCH (n {_ora17w: $marker}) DETACH DELETE n", marker=marker)


# ── Postgres: bind_organisation_guc activates RLS isolation at runtime ───────


def test_org_guc_isolates_postgres_reads_and_writes(postgres_dsn: str) -> None:
    import psycopg
    from oraclous_governance.propagation import use_organisation_context
    from oraclous_substrate.access import bind_organisation_guc
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
        # violates the RLS WITH CHECK — org B cannot write into org A's scope. The
        # violation is SQLSTATE 42501 (InsufficientPrivilege), not any psycopg error.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction(), conn.cursor() as cur:
                with use_organisation_context(_ctx(ORG_B)):
                    bind_organisation_guc(cur)
                cur.execute(insert_sql, (str(ORG_A), str(uuid.uuid4()), "smuggled"))

        # FAIL-CLOSED at the data layer: with NO org GUC bound, RLS exposes no rows
        # (absent scope denies, never defaults — AC#2 / T1-M1).
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.knowledge_graphs")
            assert cur.fetchone()[0] == 0
