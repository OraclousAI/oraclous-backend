"""MySQL reader connector (domain layer; reshape of legacy
``oraclous-core-service/app/tools/implementations/ingestion/mysql_reader.py``).

A real connector: opens an aiomysql connection from the resolved ``connection_string`` and runs read
operations. **Queries are always parameterized** — user values are bound via aiomysql's ``%s``
placeholders, never string-interpolated into SQL. Operations: ``list_tables`` and ``query``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlparse

import aiomysql

from oraclous_capability_registry_service.domain.executors.base import (
    DatabaseTool,
    ExecutionContext,
    ExecutionResult,
)


def _parse_dsn(dsn: str) -> dict[str, Any]:
    """Parse a ``mysql://user:pass@host:port/db`` connection string into aiomysql kwargs."""
    parsed = urlparse(dsn)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else "",
        "db": parsed.path.lstrip("/") or None,
    }


class MySQLReader(DatabaseTool):
    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        operation = input_data.get("operation", "query")
        conn = await aiomysql.connect(**_parse_dsn(self.get_connection_string(context)))
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                if operation == "list_tables":
                    await cur.execute("SHOW TABLES")
                    rows = await cur.fetchall()
                    tables = [next(iter(r.values())) for r in rows]
                    return ExecutionResult(
                        success=True, data={"tables": tables}, metadata={"row_count": len(tables)}
                    )
                if operation == "query":
                    sql = input_data.get("query")
                    if not isinstance(sql, str) or not sql.strip():
                        return ExecutionResult(
                            success=False,
                            error_message="'query' is required for the query operation",
                            error_type="INVALID_INPUT",
                        )
                    params = input_data.get("parameters", [])
                    if not isinstance(params, list):
                        return ExecutionResult(
                            success=False,
                            error_message="'parameters' must be a list (bound as %s placeholders)",
                            error_type="INVALID_INPUT",
                        )
                    # aiomysql binds params via %s — values are NEVER interpolated into the SQL.
                    await cur.execute(sql, tuple(params))
                    rows = await cur.fetchall()
                    data = [dict(r) for r in rows]
                    return ExecutionResult(
                        success=True,
                        data={"rows": data, "headers": list(data[0].keys()) if data else []},
                        metadata={"row_count": len(data)},
                    )
                return ExecutionResult(
                    success=False,
                    error_message=f"unsupported operation '{operation}'",
                    error_type="INVALID_OPERATION",
                )
        finally:
            conn.close()
