"""R0.5 organisation-boundary release gate — substrate level (ORA-20 / B2).

This is **the** R0.5 release gate (the brief: "R0.5 ships nothing until it is
green"). It proves the five organisation-boundary denials *at the data layer*
against real Neo4j / Postgres / Redis on the 0d harness (ORA-12):

    1. cross-organisation read denial      (Postgres, RLS driven from the org-context)
    2. cross-organisation write denial      (Postgres, RLS WITH CHECK + hidden-row UPDATE/DELETE)
    3. cross-organisation Cypher-traversal denial (Neo4j, org-scoped traversal; cross-org
       ReBAC federation is the A3/ORA-18 retriever seam, deferred there)
    4. cross-organisation search denial      (Neo4j fulltext, org-scoped)
    5. cross-organisation cache miss          (Redis, organisation-then-graph scoped keys)

plus, on the graph write path, that ``scoped_write_node`` tenants every node by the
bound organisation and ignores a caller-supplied ``organisation_id`` (T1-M1, graph half),
and the binding fail-closed criterion: with no organisation context bound, every scoped
substrate access raises rather than defaulting to some organisation.

**Binding acceptance criterion (ORA-20).** Each denial is asserted against a real
store at the data layer: a principal bound to organisation A cannot touch
organisation B's rows *regardless of request shape* (even when the request names
B's id explicitly). A mocked-DB HTTP 403 does NOT satisfy this gate, and this
suite deliberately does NOT reuse the B1 (ORA-19) mocked API-403 pattern — that
suite asserts at the API/authz layer; this one asserts at the substrate.

**TDD state.** RED until Epic A lands, by design (ADR-010):
  * A1 (ORA-16) — ``oraclous_substrate.schema.{postgres,neo4j}``, ``.cache_keys``,
    ``.organisation`` (the org-scoped schema, RLS policy, cache keys).
  * A2 (ORA-17) — ``oraclous_substrate.access`` (PROPOSED below): the query/write
    paths that take ``organisation_id`` from the authenticated org-context
    (``oraclous_governance``) — never from a request body — and apply it to every
    store, failing closed when no context is bound.
  * A3 (ORA-18) — the retriever/writer org-scoping the search + traversal denials
    exercise.

----------------------------------------------------------------------------
PROPOSED A2/A3 ENFORCEMENT SEAM — ``oraclous_substrate.access``
----------------------------------------------------------------------------
The gate drives enforcement through one cohesive substrate access module. It is
the test-author's proposed seam for A2/ORA-17 and is flagged for solution-architect
(who owns the A2 shape) and security-architect (who co-signs this gate, threat T1)
to ratify or redirect at Tests Review. If redirected, only the imports here change;
the behavioural assertions stand. Every function below reads the bound
``OrganisationContext`` via ``oraclous_governance.propagation`` and fails closed
(propagates ``MissingOrganisationContextError``) when none is bound:

  * ``scoped_pg_connection(dsn)`` — context manager yielding a psycopg connection
    with the ``app.current_organisation_id`` GUC (``schema.postgres.ORG_GUC``) set
    from the org-context, so the A1 RLS policy enforces tenancy.
  * ``scoped_write_node(driver, *, label, properties)`` — CREATE a node, stamping
    ``organisation_id`` from the org-context and overwriting any caller-supplied
    organisation_id (the tenancy field is never accepted from the caller).
  * ``scoped_traverse(driver, *, label, marker)`` — an org-scoped Cypher traversal
    that never returns nodes outside the bound organisation.
  * ``scoped_fulltext_search(driver, *, index_name, query)`` — a fulltext search
    whose hits are confined to the bound organisation.
  * ``scoped_cache_set / scoped_cache_get(redis, *, graph_id, query_text,
    retriever_type[, value])`` — Redis cache I/O keyed organisation-then-graph
    (over ``schema``-side ``cache_keys``).

----------------------------------------------------------------------------
HARNESS NOTE — RLS requires a non-superuser role
----------------------------------------------------------------------------
Postgres superusers (and BYPASSRLS roles) bypass row-level security entirely, and
the 0d harness container connects as a superuser. For RLS to actually bite, the
gate creates a dedicated NOSUPERUSER / NOBYPASSRLS application role and points the
scoped connection at it — standing in for the substrate's real application role.
A2/ORA-17 owns defining that production role; this is flagged for the architect.

Threats: T1 — M1 (org-context fail-closed) + M3 (substrate org-scoping). ADR-006
(organisation as outermost tenancy unit), ADR-004 (federation via ReBAC traversal).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = [
    pytest.mark.security,
    pytest.mark.integration,
    pytest.mark.isolation,
    pytest.mark.organization_isolation,
]

_APP_ROLE = "oraclous_gate_app"
_APP_PASSWORD = "gate"  # noqa: S105 — ephemeral test-container role, not a real secret
_ENTITY_LABEL = "__Entity__"
_FULLTEXT_INDEX = "ora20_chunk_ft"


# ---------------------------------------------------------------------------
# Organisation context — bind a resolved OrganisationContext (governance, ratified)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def org_context(organisation_id: uuid.UUID) -> Iterator[None]:
    """Bind ``organisation_id`` as the authenticated scope for the block."""
    from oraclous_governance.context import OrganisationContext, PrincipalType
    from oraclous_governance.propagation import use_organisation_context

    ctx = OrganisationContext(
        organisation_id=organisation_id,
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )
    with use_organisation_context(ctx):
        yield


@pytest.fixture
def two_orgs() -> tuple[uuid.UUID, uuid.UUID]:
    """Two distinct organisation ids, fresh per test so tests stay order-independent."""
    return uuid.uuid4(), uuid.uuid4()


# ---------------------------------------------------------------------------
# Postgres harness: apply the A1 schema and expose a non-superuser app DSN so RLS
# is actually enforced (superusers bypass RLS).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_dsn(postgres_dsn: str) -> str:
    import psycopg
    from oraclous_substrate.schema import postgres as pg_schema

    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        pg_schema.apply(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oraclous_gate_app') THEN "
                "CREATE ROLE oraclous_gate_app LOGIN PASSWORD 'gate' "
                "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; "
                "END IF; END $$;"
            )
            cur.execute("GRANT USAGE ON SCHEMA public TO oraclous_gate_app")
            cur.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE "
                "ON ALL TABLES IN SCHEMA public TO oraclous_gate_app"
            )

    parts = urlsplit(postgres_dsn)
    netloc = f"{_APP_ROLE}:{_APP_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# ---------------------------------------------------------------------------
# 1. Cross-organisation READ denial (Postgres / RLS driven by the org-context)
# ---------------------------------------------------------------------------


def test_cross_organisation_read_is_denied_at_the_data_layer(
    app_dsn: str, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Organisation A's reads never see organisation B's rows — even when A's
    request names B's id or organisation_id explicitly (RLS scopes by the GUC
    that the access layer takes from the authenticated context, not the request)."""
    from oraclous_substrate.access import scoped_pg_connection

    org_a, org_b = two_orgs

    # Organisation B seeds a row it owns, under B's own authenticated context.
    with org_context(org_b), scoped_pg_connection(app_dsn) as conn_b:
        with conn_b.cursor() as cur:
            cur.execute(
                "INSERT INTO knowledge_graphs (organisation_id, user_id, name) "
                "VALUES (%s, %s, %s) RETURNING id",
                (str(org_b), str(uuid.uuid4()), "org-b-secret"),
            )
            (row_b_id,) = cur.fetchone()
        conn_b.commit()

    # Organisation A seeds a row of its own, so the read below is a positive+negative
    # proof: A must see exactly its own row and none of B's — not merely "A sees nothing".
    with org_context(org_a), scoped_pg_connection(app_dsn) as conn_a:
        with conn_a.cursor() as cur:
            cur.execute(
                "INSERT INTO knowledge_graphs (organisation_id, user_id, name) "
                "VALUES (%s, %s, %s) RETURNING id",
                (str(org_a), str(uuid.uuid4()), "org-a-own"),
            )
            (row_a_id,) = cur.fetchone()
        conn_a.commit()

    # Organisation A, bound to its own context, sees exactly its own row by any shape.
    with org_context(org_a), scoped_pg_connection(app_dsn) as conn_a:
        with conn_a.cursor() as cur:
            cur.execute("SELECT count(*) FROM knowledge_graphs WHERE id = %s", (row_b_id,))
            assert cur.fetchone()[0] == 0, "org A read org B's row by id — RLS not enforced"

            cur.execute(
                "SELECT count(*) FROM knowledge_graphs WHERE organisation_id = %s",
                (str(org_b),),
            )
            assert cur.fetchone()[0] == 0, "naming org B's organisation_id still leaked the row"

            cur.execute("SELECT id FROM knowledge_graphs")
            visible = {row[0] for row in cur.fetchall()}
            assert visible == {row_a_id}, (
                "org A's unfiltered SELECT did not return exactly its own row "
                f"(saw {visible!r}) — RLS is over- or under-scoping"
            )


# ---------------------------------------------------------------------------
# 2. Cross-organisation WRITE denial (Postgres / RLS WITH CHECK + hidden rows)
# ---------------------------------------------------------------------------


def test_cross_organisation_write_is_denied_at_the_data_layer(
    app_dsn: str, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Organisation A cannot create a row tagged for organisation B, nor modify or
    delete B's existing rows — the data layer rejects or no-ops the cross-org write."""
    import psycopg
    from oraclous_substrate.access import scoped_pg_connection

    org_a, org_b = two_orgs

    with org_context(org_b), scoped_pg_connection(app_dsn) as conn_b:
        with conn_b.cursor() as cur:
            cur.execute(
                "INSERT INTO knowledge_graphs (organisation_id, user_id, name) "
                "VALUES (%s, %s, %s) RETURNING id",
                (str(org_b), str(uuid.uuid4()), "org-b-owned"),
            )
            (row_b_id,) = cur.fetchone()
        conn_b.commit()

    # A fresh connection per step so the GUC is set cleanly regardless of how the
    # access layer manages it across an aborted transaction.
    with org_context(org_a), scoped_pg_connection(app_dsn) as conn_a:
        with conn_a.cursor() as cur, pytest.raises(psycopg.errors.Error) as exc:
            cur.execute(
                "INSERT INTO knowledge_graphs (organisation_id, user_id, name) VALUES (%s, %s, %s)",
                (str(org_b), str(uuid.uuid4()), "smuggled-into-org-b"),
            )
        assert exc.value.sqlstate == "42501", (
            "cross-org INSERT was not rejected by the RLS row-security policy "
            f"(sqlstate={exc.value.sqlstate})"
        )

    with org_context(org_a), scoped_pg_connection(app_dsn) as conn_a:
        with conn_a.cursor() as cur:
            cur.execute("UPDATE knowledge_graphs SET name = 'hijacked' WHERE id = %s", (row_b_id,))
            assert cur.rowcount == 0, "org A updated a row belonging to org B"
            cur.execute("DELETE FROM knowledge_graphs WHERE id = %s", (row_b_id,))
            assert cur.rowcount == 0, "org A deleted a row belonging to org B"
        conn_a.commit()

    # Organisation B's row is intact and unmodified.
    with org_context(org_b), scoped_pg_connection(app_dsn) as conn_b:
        with conn_b.cursor() as cur:
            cur.execute("SELECT name FROM knowledge_graphs WHERE id = %s", (row_b_id,))
            assert cur.fetchone()[0] == "org-b-owned"


# ---------------------------------------------------------------------------
# 3. Cross-organisation CYPHER-TRAVERSAL denial (Neo4j + ReBAC federation)
# ---------------------------------------------------------------------------


def _seed_org_path(
    driver, *, organisation_id: uuid.UUID, marker: str, start_name: str, end_name: str
) -> None:
    driver.execute_query(
        f"CREATE (a:`{_ENTITY_LABEL}` {{organisation_id: $org, marker: $m, name: $sn}})"
        f"-[:RELATED {{organisation_id: $org, marker: $m}}]->"
        f"(b:`{_ENTITY_LABEL}` {{organisation_id: $org, marker: $m, name: $en}})",
        org=str(organisation_id),
        m=marker,
        sn=start_name,
        en=end_name,
    )


def test_cross_organisation_cypher_traversal_is_denied(
    neo4j_driver, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """An org-scoped Cypher traversal never crosses the organisation boundary: org A's
    traversal reaches its own connected nodes and none of org B's."""
    from oraclous_substrate.access import scoped_traverse

    org_a, org_b = two_orgs
    marker = f"ora20-{uuid.uuid4()}"
    try:
        _seed_org_path(
            neo4j_driver,
            organisation_id=org_a,
            marker=marker,
            start_name="a-start",
            end_name="a-end",
        )
        _seed_org_path(
            neo4j_driver,
            organisation_id=org_b,
            marker=marker,
            start_name="b-start",
            end_name="b-end",
        )

        with org_context(org_a):
            names = {
                row["name"]
                for row in scoped_traverse(neo4j_driver, label=_ENTITY_LABEL, marker=marker)
            }

        assert "a-end" in names, "org A's traversal did not reach its own connected node"
        assert "b-end" not in names, "org A's traversal reached org B's node — boundary crossed"
        assert "b-start" not in names, "org A's traversal surfaced org B's node"

        # NB: whether scoped_traverse *consults* ReBAC on a cross-organisation request
        # (ADR-004 fail-closed federation, T1-M2) is the cross-org traversal path, which
        # A3/ORA-18 owns and asserts at the retriever seam. This gate proves the intra-org
        # data-layer scoping it owns. The earlier unit-level AccessDecisionClient sub-assertion
        # here duplicated packages/substrate/tests/unit/test_rebac_client.py and was dropped at
        # Tests Review (be-test-reviewer + security-architect — accepted residual on T1-M2).
    finally:
        neo4j_driver.execute_query(
            f"MATCH (n:`{_ENTITY_LABEL}` {{marker: $m}}) DETACH DELETE n", m=marker
        )


# ---------------------------------------------------------------------------
# 4. Cross-organisation SEARCH denial (Neo4j fulltext, org-scoped)
# ---------------------------------------------------------------------------


def test_cross_organisation_search_is_denied(
    neo4j_driver, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A fulltext search bound to organisation A never returns organisation B's
    documents, even when the query term occurs only in B's content."""
    from oraclous_substrate.access import scoped_fulltext_search, scoped_write_node

    org_a, org_b = two_orgs
    marker = f"ora20-{uuid.uuid4()}"
    a_term = f"alpha{uuid.uuid4().hex[:8]}"
    b_term = f"bravo{uuid.uuid4().hex[:8]}"

    neo4j_driver.execute_query(
        f"CREATE FULLTEXT INDEX {_FULLTEXT_INDEX} IF NOT EXISTS FOR (n:Chunk) ON EACH [n.text]"
    )
    neo4j_driver.execute_query("CALL db.awaitIndexes()")
    try:
        with org_context(org_a):
            scoped_write_node(
                neo4j_driver, label="Chunk", properties={"marker": marker, "text": f"{a_term} body"}
            )
        with org_context(org_b):
            scoped_write_node(
                neo4j_driver, label="Chunk", properties={"marker": marker, "text": f"{b_term} body"}
            )
        neo4j_driver.execute_query("CALL db.awaitIndexes()")

        with org_context(org_a):
            hits_for_b = scoped_fulltext_search(
                neo4j_driver, index_name=_FULLTEXT_INDEX, query=b_term
            )
            hits_for_a = scoped_fulltext_search(
                neo4j_driver, index_name=_FULLTEXT_INDEX, query=a_term
            )

        assert hits_for_b == [], "org A's search surfaced org B's document"
        assert any(a_term in hit.get("text", "") for hit in hits_for_a), (
            "org A's search did not find its own document"
        )
    finally:
        neo4j_driver.execute_query("MATCH (n:Chunk {marker: $m}) DETACH DELETE n", m=marker)


# ---------------------------------------------------------------------------
# Graph-store WRITE denial (Neo4j): organisation_id is stamped from the bound
# context and is never honoured from the caller's payload (T1-M1, graph half).
# The Postgres write path is covered above; this is the legacy MultiTenantKGWriter
# tenant boundary — the writer unconditionally overwrites organisation_id.
# ---------------------------------------------------------------------------


def test_scoped_write_node_stamps_org_from_context_ignoring_caller(
    neo4j_driver, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A node written through ``scoped_write_node`` carries the bound organisation's id,
    taken from the authenticated context — never the request body. A caller that smuggles
    a foreign ``organisation_id`` into the node payload does not land the node in that
    organisation's tenant space (T1-M1 on the graph write path)."""
    from oraclous_substrate.access import scoped_write_node

    org_a, org_b = two_orgs
    marker = f"ora20-{uuid.uuid4()}"
    try:
        # Org A writes a node while smuggling org B's id into the caller-supplied payload.
        with org_context(org_a):
            scoped_write_node(
                neo4j_driver,
                label=_ENTITY_LABEL,
                properties={
                    "organisation_id": str(org_b),
                    "marker": marker,
                    "name": "smuggled",
                },
            )

        # The persisted node is stamped for org A (its context), not org B (the payload).
        records, _, _ = neo4j_driver.execute_query(
            f"MATCH (n:`{_ENTITY_LABEL}` {{marker: $m}}) RETURN n.organisation_id AS org",
            m=marker,
        )
        stamped = sorted(r["org"] for r in records)
        assert stamped == [str(org_a)], (
            "scoped_write_node honoured a caller-supplied organisation_id "
            f"(node tenanted as {stamped!r}, expected only org A {str(org_a)!r})"
        )
        assert str(org_b) not in stamped, (
            "node was smuggled into org B's tenant space via the caller payload"
        )
    finally:
        neo4j_driver.execute_query(
            f"MATCH (n:`{_ENTITY_LABEL}` {{marker: $m}}) DETACH DELETE n", m=marker
        )


# ---------------------------------------------------------------------------
# 5. Cross-organisation CACHE MISS (Redis, organisation-then-graph scoped keys)
# ---------------------------------------------------------------------------


def test_cross_organisation_cache_lookup_misses(
    redis_client, two_orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A cache entry written by organisation B is a miss for organisation A on the
    same query (the cache key is organisation-scoped), while each org reads its own."""
    from oraclous_substrate.access import scoped_cache_get, scoped_cache_set

    org_a, org_b = two_orgs
    graph = f"graph-{uuid.uuid4()}"
    query = "what is the quarterly revenue?"
    retriever = "graphrag"
    try:
        with org_context(org_b):
            scoped_cache_set(
                redis_client,
                graph_id=graph,
                query_text=query,
                retriever_type=retriever,
                value="org-b-answer",
            )

        with org_context(org_a):
            assert (
                scoped_cache_get(
                    redis_client, graph_id=graph, query_text=query, retriever_type=retriever
                )
                is None
            ), "org A hit a cache entry that org B wrote"
            scoped_cache_set(
                redis_client,
                graph_id=graph,
                query_text=query,
                retriever_type=retriever,
                value="org-a-answer",
            )
            assert (
                scoped_cache_get(
                    redis_client, graph_id=graph, query_text=query, retriever_type=retriever
                )
                == "org-a-answer"
            ), "org A could not read back its own cache entry"

        with org_context(org_b):
            assert (
                scoped_cache_get(
                    redis_client, graph_id=graph, query_text=query, retriever_type=retriever
                )
                == "org-b-answer"
            ), "org A's write clobbered org B's cache entry"
    finally:
        from oraclous_substrate.cache_keys import query_cache_pattern

        for org in (org_a, org_b):
            cursor = 0
            pattern = query_cache_pattern(str(org), graph)
            while True:
                cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    redis_client.delete(*keys)
                if cursor == 0:
                    break


# ---------------------------------------------------------------------------
# 6. Fail-closed: no organisation context bound (the binding criterion, T1-M1)
# ---------------------------------------------------------------------------


def test_substrate_access_without_organisation_context_fails_closed(
    app_dsn: str, neo4j_driver, redis_client
) -> None:
    """With no organisation context bound, every scoped substrate access raises
    rather than defaulting to some organisation (fail-closed; never widen access)."""
    from oraclous_governance.propagation import MissingOrganisationContextError
    from oraclous_substrate.access import (
        scoped_cache_get,
        scoped_cache_set,
        scoped_fulltext_search,
        scoped_pg_connection,
        scoped_traverse,
        scoped_write_node,
    )

    with pytest.raises(MissingOrganisationContextError):
        with scoped_pg_connection(app_dsn):
            pass

    with pytest.raises(MissingOrganisationContextError):
        scoped_write_node(neo4j_driver, label=_ENTITY_LABEL, properties={"name": "x"})

    with pytest.raises(MissingOrganisationContextError):
        scoped_traverse(neo4j_driver, label=_ENTITY_LABEL, marker="none")

    with pytest.raises(MissingOrganisationContextError):
        scoped_fulltext_search(neo4j_driver, index_name=_FULLTEXT_INDEX, query="x")

    with pytest.raises(MissingOrganisationContextError):
        scoped_cache_get(redis_client, graph_id="g", query_text="q", retriever_type="graphrag")

    with pytest.raises(MissingOrganisationContextError):
        scoped_cache_set(
            redis_client, graph_id="g", query_text="q", retriever_type="graphrag", value="v"
        )
