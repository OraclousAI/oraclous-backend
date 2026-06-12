"""SQL database connector — PostgreSQL (asyncpg) + MySQL (aiomysql) relational ingest source.

ORAA-4 §21: this lives under ``domain/connectors/`` (the STR004-exempt connector layer — its
asyncpg/aiomysql driver use is the OUTBOUND payload to an EXTERNAL user DB, not the service's own
persistence, which stays in ``repositories/``). Lift-and-reshape of legacy ``develop@84152635
knowledge-graph-builder/app/services/database_connector_service.py`` (the PostgreSQL + MySQL halves;
MongoDB and the CDC sync mode are dropped — out of scope for #307).

Two read operations:

  * :meth:`introspect_schema` — query ``information_schema`` for tables / columns / PK / FK and
    return a :class:`SchemaSnapshot` (no row data). An FK column whose ``fk_table`` is set carries
    the target for the ``REFERENCES_{TARGET}`` relationship the recipe projects.
  * :meth:`fetch_rows` — return rows of ONE table as plain dicts. The table name is validated
    against the introspected snapshot allowlist (never user input) before it reaches the SQL string;
    standard SQL identifier quoting then prevents injection from names with special characters, and
    the only bound value (the row limit) is a parameterized placeholder.

Sync modes (mirroring the legacy):
  * ``full_snapshot`` — introspect + fetch rows (the relational ingest projects rows → entities).
  * ``schema_only`` — introspect only (metadata; the recipe projects table/column structure).

The host is validated by the TCP egress guard (:func:`...domain.tcp_egress.validate_db_host`) BEFORE
any connect, and the connection is opened against the PINNED resolved IP (so the validated address
is the one actually dialed — mitigating the DNS-rebinding TOCTOU). Credentials are NEVER stored
here: the DSN is resolved from the broker by ``credential_id`` at ingest-request time and passed in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import unquote, urlparse

from oraclous_knowledge_graph_service.domain.tcp_egress import validate_db_host


class SqlDialect(StrEnum):
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"


class DbSyncMode(StrEnum):
    FULL_SNAPSHOT = "full_snapshot"
    SCHEMA_ONLY = "schema_only"


# Scheme aliases a DSN may carry → dialect.
_SCHEME_TO_DIALECT: dict[str, SqlDialect] = {
    "postgresql": SqlDialect.POSTGRESQL,
    "postgres": SqlDialect.POSTGRESQL,
    "mysql": SqlDialect.MYSQL,
    "mariadb": SqlDialect.MYSQL,
}
_DEFAULT_PORT: dict[SqlDialect, int] = {SqlDialect.POSTGRESQL: 5432, SqlDialect.MYSQL: 3306}


class SqlConnectorError(Exception):
    """A SQL connector operation failed (bad DSN, unsupported dialect, connect/introspect error)."""


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    data_type: str
    nullable: bool
    is_pk: bool
    is_fk: bool
    fk_table: str | None = None
    fk_column: str | None = None


@dataclass(frozen=True)
class TableMeta:
    name: str
    schema_name: str
    columns: list[ColumnMeta] = field(default_factory=list)


@dataclass(frozen=True)
class SchemaSnapshot:
    dialect: SqlDialect
    database: str
    schema_name: str
    tables: list[TableMeta]


@dataclass(frozen=True)
class DbConnParams:
    """Connection parameters parsed from a resolved DSN (+ the egress-validated pinned IP)."""

    dialect: SqlDialect
    host: str  # the original hostname (for messages / TLS SNI)
    pinned_ip: str  # the egress-validated resolved IP to actually dial
    port: int
    user: str | None
    password: str
    database: str


def parse_and_validate_dsn(dsn: str, *, allow_private: bool) -> DbConnParams:
    """Parse a ``<scheme>://user:pass@host:port/db`` DSN, run the egress guard on its host, and
    return the connection params with the pinned resolved IP. Fail-closed: an unparseable DSN, an
    unsupported scheme, a missing host, or a blocked host all raise (the latter via the guard)."""
    try:
        parsed = urlparse(dsn)
    except (ValueError, TypeError) as exc:
        raise SqlConnectorError(f"unparseable connection string: {exc}") from exc
    scheme = (parsed.scheme or "").lower().split("+", 1)[0]  # strip a `+asyncpg`/`+pymysql` driver
    dialect = _SCHEME_TO_DIALECT.get(scheme)
    if dialect is None:
        raise SqlConnectorError(
            f"unsupported connection-string scheme {scheme or '(none)'!r} "
            "(expected postgresql:// or mysql://)"
        )
    host = parsed.hostname
    if not host:
        raise SqlConnectorError("connection string has no host")
    database = (parsed.path or "").lstrip("/")
    if not database:
        raise SqlConnectorError("connection string has no database/path component")
    # Egress guard: validate the host + pin the resolved IP we will actually connect to.
    pinned_ip = validate_db_host(host, allow_private=allow_private)
    return DbConnParams(
        dialect=dialect,
        host=host,
        pinned_ip=pinned_ip,
        port=parsed.port or _DEFAULT_PORT[dialect],
        user=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else "",
        database=database,
    )


def map_fk_relationship_type(fk_table: str) -> str:
    """An FK whose target table is ``fk_table`` maps to a typed ``REFERENCES_{TARGET}`` relationship
    (the target rendered UPPER_SNAKE). Cypher-safe by construction — the recipe engine + writer
    re-validate the identifier at the write boundary (defense in depth)."""
    import re

    target = re.sub(r"[^0-9A-Za-z_]", "_", fk_table).strip("_").upper()
    rel = f"REFERENCES_{target}" if target else "REFERENCES"
    if not re.match(r"^[A-Z_][A-Z0-9_]*$", rel):
        rel = "REFERENCES"
    return rel


# --- introspection SQL --------------------------------------------------------
_PG_INTROSPECT = """
SELECT
    c.table_name,
    c.column_name,
    c.data_type,
    c.is_nullable,
    CASE WHEN pk.column_name IS NOT NULL THEN TRUE ELSE FALSE END AS is_pk,
    CASE WHEN fk.column_name IS NOT NULL THEN TRUE ELSE FALSE END AS is_fk,
    fk.foreign_table_name AS fk_table,
    fk.foreign_column_name AS fk_column
FROM information_schema.columns c
LEFT JOIN (
    SELECT ku.column_name, ku.table_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage ku
      ON tc.constraint_name = ku.constraint_name AND tc.table_schema = ku.table_schema
    WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = $1
) pk ON pk.table_name = c.table_name AND pk.column_name = c.column_name
LEFT JOIN (
    SELECT
        ku.column_name, ku.table_name,
        ccu.table_name AS foreign_table_name,
        ccu.column_name AS foreign_column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage ku
      ON tc.constraint_name = ku.constraint_name AND tc.table_schema = ku.table_schema
    JOIN information_schema.referential_constraints rc
      ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
    JOIN information_schema.constraint_column_usage ccu
      ON rc.unique_constraint_name = ccu.constraint_name
      AND rc.unique_constraint_schema = ccu.constraint_schema
    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = $1
) fk ON fk.table_name = c.table_name AND fk.column_name = c.column_name
WHERE c.table_schema = $1
ORDER BY c.table_name, c.ordinal_position
"""

_MYSQL_INTROSPECT = """
SELECT
    c.TABLE_NAME     AS table_name,
    c.COLUMN_NAME    AS column_name,
    c.DATA_TYPE      AS data_type,
    c.IS_NULLABLE    AS is_nullable,
    CASE WHEN c.COLUMN_KEY = 'PRI' THEN 1 ELSE 0 END AS is_pk,
    CASE WHEN k.REFERENCED_TABLE_NAME IS NOT NULL THEN 1 ELSE 0 END AS is_fk,
    k.REFERENCED_TABLE_NAME  AS fk_table,
    k.REFERENCED_COLUMN_NAME AS fk_column
FROM INFORMATION_SCHEMA.COLUMNS c
LEFT JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
  ON  c.TABLE_SCHEMA  = k.TABLE_SCHEMA
  AND c.TABLE_NAME    = k.TABLE_NAME
  AND c.COLUMN_NAME   = k.COLUMN_NAME
  AND k.REFERENCED_TABLE_NAME IS NOT NULL
WHERE c.TABLE_SCHEMA = %s
ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
"""


class SqlConnector:
    """Async PostgreSQL/MySQL connector. One instance per ingest; ``connect()`` then ``close()``.

    The SCHEMA introspection is dialect-aware; ``fetch_rows`` quotes identifiers per dialect and
    validates the table name against the introspected snapshot allowlist before it is used.
    """

    def __init__(self, params: DbConnParams, *, schema: str | None = None) -> None:
        self._p = params
        # Postgres has a schema namespace (default ``public``); MySQL's "schema" IS the database.
        self._schema = schema or (
            "public" if params.dialect is SqlDialect.POSTGRESQL else params.database
        )
        self._conn: Any = None

    async def connect(self) -> None:
        if self._p.dialect is SqlDialect.POSTGRESQL:
            import asyncpg

            # Connect to the PINNED, egress-validated IP (not the hostname) to mitigate DNS
            # rebinding; pass the original host as `server_settings`-free — asyncpg dials `host`.
            self._conn = await asyncpg.connect(
                host=self._p.pinned_ip,
                port=self._p.port,
                user=self._p.user,
                password=self._p.password,
                database=self._p.database,
                timeout=10,
                command_timeout=30,
            )
        else:
            import aiomysql

            self._conn = await aiomysql.connect(
                host=self._p.pinned_ip,
                port=self._p.port,
                user=self._p.user,
                password=self._p.password,
                db=self._p.database,
                connect_timeout=10,
            )

    async def introspect_schema(self) -> SchemaSnapshot:
        if self._conn is None:
            raise SqlConnectorError("not connected; call connect() first")
        if self._p.dialect is SqlDialect.POSTGRESQL:
            rows = [dict(r) for r in await self._conn.fetch(_PG_INTROSPECT, self._schema)]
        else:
            import aiomysql

            async with self._conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(_MYSQL_INTROSPECT, (self._schema,))
                rows = [dict(r) for r in await cur.fetchall()]

        tables: dict[str, list[ColumnMeta]] = {}
        for row in rows:
            tname = row["table_name"]
            tables.setdefault(tname, []).append(
                ColumnMeta(
                    name=row["column_name"],
                    data_type=str(row["data_type"]),
                    nullable=str(row["is_nullable"]).upper() == "YES",
                    is_pk=bool(row["is_pk"]),
                    is_fk=bool(row["is_fk"]),
                    fk_table=row["fk_table"],
                    fk_column=row["fk_column"],
                )
            )
        return SchemaSnapshot(
            dialect=self._p.dialect,
            database=self._p.database,
            schema_name=self._schema,
            tables=[
                TableMeta(name=t, schema_name=self._schema, columns=c) for t, c in tables.items()
            ],
        )

    async def fetch_rows(
        self, table: str, snapshot: SchemaSnapshot, *, limit: int = 10_000
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` rows of ``table`` as plain dicts. ``table`` MUST be in the
        introspected snapshot's allowlist (it comes from introspection, never raw user input)."""
        if self._conn is None:
            raise SqlConnectorError("not connected; call connect() first")
        allowed = {t.name for t in snapshot.tables}
        if table not in allowed:
            raise SqlConnectorError(
                f"table {table!r} is not in the introspected schema allowlist "
                "(table names must come from introspection, never user input)"
            )
        if self._p.dialect is SqlDialect.POSTGRESQL:
            # S608 false positive: `table`/`self._schema` are NOT user input — they are introspected
            # identifiers, allowlist-checked above; double-quoting protects special chars; the limit
            # is a bound parameterized value ($1), never interpolated.
            rows = await self._conn.fetch(
                f'SELECT * FROM "{self._schema}"."{table}" LIMIT $1',  # noqa: S608
                limit,
            )
            return [dict(r) for r in rows]
        import aiomysql

        async with self._conn.cursor(aiomysql.DictCursor) as cur:
            # S608 false positive: same allowlist+quoting contract; backtick-quoting protects the
            # introspected name; the limit is a bound %s value.
            await cur.execute(
                f"SELECT * FROM `{self._p.database}`.`{table}` LIMIT %s",  # noqa: S608
                (limit,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def close(self) -> None:
        if self._conn is not None:
            if self._p.dialect is SqlDialect.POSTGRESQL:
                await self._conn.close()
            else:
                self._conn.close()
            self._conn = None
