"""Substrate-level organisation-isolation pattern, proven at the data layer
(ORA-12 / 0d).

This is the harness demonstration, not the R0.5 release gate. It stands up two
organisations' rows in a real Postgres and shows that a query filtered by
``organisation_id`` never returns another organisation's data. The real Epic-A/B
gate enforces this via row-level security + the org-context across every store;
this proves the harness can assert such things at the data layer.
"""

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]


def test_postgres_rows_scoped_by_organisation(postgres_dsn: str) -> None:
    import psycopg

    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS thing "
            "(id uuid PRIMARY KEY, organisation_id uuid NOT NULL, name text)"
        )
        cur.execute(
            "INSERT INTO thing (id, organisation_id, name) VALUES (%s, %s, %s), (%s, %s, %s)",
            (uuid.uuid4(), org_a, "a-thing", uuid.uuid4(), org_b, "b-thing"),
        )
        conn.commit()
        cur.execute("SELECT name FROM thing WHERE organisation_id = %s", (str(org_a),))
        rows = [r[0] for r in cur.fetchall()]

    assert rows == ["a-thing"]  # org A's query never sees org B's row
