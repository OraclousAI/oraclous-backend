"""PostgreSQL reader connector (domain layer; reshape of legacy
``oraclous-core-service/app/tools/implementations/ingestion/postgresql_reader.py``).

A real connector: it opens an asyncpg connection from the resolved ``connection_string`` credential
and runs read operations. **Queries are always parameterized** — user values are bound as ``$1,$2…``
via asyncpg and never string-interpolated into SQL (SQL-injection protection). Supported operations:
``list_tables`` and ``query``.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from oraclous_capability_registry_service.domain.executors.base import (
    DatabaseTool,
    ExecutionContext,
    ExecutionResult,
)


class PostgreSQLReader(DatabaseTool):
    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        operation = input_data.get("operation", "query")
        dsn = self.get_connection_string(context)
        conn = await asyncpg.connect(dsn)
        try:
            if operation == "list_tables":
                rows = await conn.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = $1 ORDER BY table_name",
                    input_data.get("schema", "public"),
                )
                tables = [r["table_name"] for r in rows]
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
                        error_message="'parameters' must be a list (bound as $1,$2…)",
                        error_type="INVALID_INPUT",
                    )
                # asyncpg binds params positionally — values are NEVER interpolated into the SQL.
                rows = await conn.fetch(sql, *params)
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
            await conn.close()
