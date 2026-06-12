"""Real-Postgres SQL relational ingest integration test (#307, ORAA-4 §22 — real substrate).

Proves the SQL connector + relational recipe path end-to-end against a REAL Postgres (a
testcontainer) and a REAL Neo4j (a testcontainer), not a mock:

  * a small schema with an FK (``departments`` ← ``employees.dept_id``) is created + seeded;
  * introspection returns the schema snapshot with the FK mapped (TableMeta/ColumnMeta + fk_table);
  * a full ingest projects rows→entities keyed by PK + ``REFERENCES_DEPARTMENTS`` relationships,
    org+graph scoped through the real org-scoped writer;
  * cross-org isolation: a second org sees NONE of the first org's nodes for the same graph_id.

The DB host is the testcontainer's IP — the egress guard runs in single-tenant (``allow_private``)
mode so the private container host is allowed (a real DB host validation, not bypassed).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import use_organisation_context

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_PG_IMAGE = "postgres:16-alpine"
_NEO4J_IMAGE = "neo4j:5.23-community"
_ORG_A = "11111111-1111-1111-1111-1111111111aa"
_ORG_B = "22222222-2222-2222-2222-2222222222bb"


@pytest.fixture(scope="module")
def source_pg_dsn() -> Iterator[str]:
    """A real Postgres SOURCE DB (the user's DB we ingest from), seeded with an FK schema."""
    import psycopg
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        _PG_IMAGE,
        username="src",
        password="srcpw",  # noqa: S106 — test container, not a real secret
        dbname="sourcedb",
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        sync_dsn = f"postgresql://src:srcpw@{host}:{port}/sourcedb"
        with psycopg.connect(sync_dsn) as conn, conn.cursor() as cur:
            cur.execute("CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            cur.execute(
                "CREATE TABLE employees ("
                "  id INTEGER PRIMARY KEY,"
                "  name TEXT NOT NULL,"
                "  dept_id INTEGER REFERENCES departments(id)"
                ")"
            )
            cur.execute("INSERT INTO departments VALUES (1, 'Engineering'), (2, 'Sales')")
            cur.execute(
                "INSERT INTO employees VALUES (10, 'Ada', 1), (11, 'Babbage', 1), (12, 'Carol', 2)"
            )
            conn.commit()
        # The asyncpg DSN the connector will use (same host:port — egress validates it).
        yield f"postgresql://src:srcpw@{host}:{port}/sourcedb"


@pytest.fixture(scope="module")
def graph_neo4j_driver() -> Iterator[object]:
    """A real Neo4j as the GRAPH substrate the recipe writer writes into."""
    from neo4j import GraphDatabase
    from testcontainers.neo4j import Neo4jContainer

    with Neo4jContainer(_NEO4J_IMAGE).with_env("NEO4J_AUTH", "neo4j/password") as container:
        driver = GraphDatabase.driver(container.get_connection_url(), auth=("neo4j", "password"))
        try:
            driver.verify_connectivity()
            yield driver
        finally:
            driver.close()


def _ctx(org: str) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=uuid.UUID(org),
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


async def _introspect(dsn: str):
    from oraclous_knowledge_graph_service.domain.connectors.sql_connector import (
        SqlConnector,
        parse_and_validate_dsn,
    )

    params = parse_and_validate_dsn(dsn, allow_private=True)
    connector = SqlConnector(params)
    await connector.connect()
    try:
        return await connector.introspect_schema()
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_introspection_maps_schema_and_fk(source_pg_dsn: str) -> None:
    snapshot = await _introspect(source_pg_dsn)
    tables = {t.name: t for t in snapshot.tables}
    assert set(tables) == {"departments", "employees"}

    dept_cols = {c.name: c for c in tables["departments"].columns}
    assert dept_cols["id"].is_pk is True

    emp_cols = {c.name: c for c in tables["employees"].columns}
    assert emp_cols["id"].is_pk is True
    # The FK is mapped: dept_id → departments.id
    assert emp_cols["dept_id"].is_fk is True
    assert emp_cols["dept_id"].fk_table == "departments"
    assert emp_cols["dept_id"].fk_column == "id"


def _count_nodes(driver, org: str, graph_id: str, label: str) -> int:
    records, _, _ = driver.execute_query(
        f"MATCH (n:{label}:__Entity__ "
        "{organisation_id: $org, graph_id: $g}) RETURN count(n) AS n",
        org=org,
        g=graph_id,
    )
    return int(records[0]["n"])


def _count_rels(driver, org: str, graph_id: str, rel_type: str) -> int:
    records, _, _ = driver.execute_query(
        f"MATCH (:__Entity__ {{organisation_id: $org, graph_id: $g}})"
        f"-[r:{rel_type} {{organisation_id: $org, graph_id: $g}}]->"
        "(:__Entity__ {organisation_id: $org, graph_id: $g}) RETURN count(r) AS n",
        org=org,
        g=graph_id,
    )
    return int(records[0]["n"])


async def _run_ingest(driver, dsn: str, org: str, graph_id: str) -> dict:
    from oraclous_knowledge_graph_service.core.config import Settings
    from oraclous_knowledge_graph_service.services.credential_client import FakeCredentialBroker
    from oraclous_knowledge_graph_service.services.sql_ingestion_service import SqlIngestionService

    broker = FakeCredentialBroker(default_dsn=dsn)
    # A fresh Settings (not the lru_cached singleton) so the flag flip never leaks into other tests.
    settings = Settings(sql_ingest_allow_private_egress=True)  # the container host is private
    service = SqlIngestionService(
        driver=driver, broker=broker, organisation_id=org, database=None, settings=settings
    )
    with use_organisation_context(_ctx(org)):
        return await service.ingest(graph_id=graph_id, credential_id="cred-src")


@pytest.mark.asyncio
async def test_full_ingest_entities_relationships_and_cross_org_isolation(
    source_pg_dsn: str, graph_neo4j_driver
) -> None:
    driver = graph_neo4j_driver
    graph_id = str(uuid.uuid4())

    # Org A ingests the source DB into the graph.
    result = await _run_ingest(driver, source_pg_dsn, _ORG_A, graph_id)
    assert result["dialect"] == "postgresql"
    assert result["tables_introspected"] == 2

    # Entities: 2 departments + 3 employees, scoped to org A.
    assert _count_nodes(driver, _ORG_A, graph_id, "Departments") == 2
    assert _count_nodes(driver, _ORG_A, graph_id, "Employees") == 3
    # FK → REFERENCES_DEPARTMENTS: one per employee.
    assert _count_rels(driver, _ORG_A, graph_id, "REFERENCES_DEPARTMENTS") == 3

    # Cross-org isolation: org B (same graph_id) sees NONE of org A's nodes.
    assert _count_nodes(driver, _ORG_B, graph_id, "Departments") == 0
    assert _count_nodes(driver, _ORG_B, graph_id, "Employees") == 0
    assert _count_rels(driver, _ORG_B, graph_id, "REFERENCES_DEPARTMENTS") == 0

    # Org B ingests the SAME source into the SAME graph_id — its nodes are its own (org-scoped).
    await _run_ingest(driver, source_pg_dsn, _ORG_B, graph_id)
    assert _count_nodes(driver, _ORG_B, graph_id, "Employees") == 3
    # Org A's counts are unchanged (no cross-org bleed).
    assert _count_nodes(driver, _ORG_A, graph_id, "Employees") == 3
