"""Smoke test proving the substrate harness brings up real Neo4j, Postgres and
Redis and that each is reachable from a test."""

import pytest

pytestmark = pytest.mark.integration


def test_neo4j_reachable(neo4j_driver) -> None:
    records, _, _ = neo4j_driver.execute_query("RETURN 1 AS one")
    assert records[0]["one"] == 1


def test_postgres_reachable(postgres_dsn: str) -> None:
    import psycopg

    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        row = cur.fetchone()
    assert row is not None and row[0] == 1


def test_redis_reachable(redis_client) -> None:
    assert redis_client.ping() is True
