from typing import Any, Dict
import asyncpg
from decimal import Decimal

from app.utils.tool_id_generator import generate_tool_id
from app.tools.base.database_tool import DatabaseTool
from app.tools.plugin import CapabilityKindPlugin, plugin_registry
from app.tools.registry import tool_registry
from app.models.capability_descriptor import DescriptorKind
from app.schemas.tool_instance import ExecutionContext, ExecutionResult
from app.schemas.tool_definition import (
    ToolDefinition,
    ToolSchema,
    ToolCapability,
    CredentialRequirement,
)
from app.schemas.common import ToolCategory, ToolType, CredentialType


class PostgreSQLReader(DatabaseTool, CapabilityKindPlugin):
    """
    Tool for reading data from PostgreSQL databases
    """

    @classmethod
    def get_tool_definition(cls) -> ToolDefinition:
        """Return the tool definition for PostgreSQL Reader"""
        return ToolDefinition(
            id=generate_tool_id("PostgreSQL Reader", "1.0.0", "INGESTION"),
            name="PostgreSQL Reader",
            description="Execute queries and read data from PostgreSQL databases",
            version="1.0.0",
            category=ToolCategory.INGESTION,
            type=ToolType.INTERNAL,
            capabilities=[
                ToolCapability(
                    name="execute_sql_query",
                    description="Execute SQL queries on PostgreSQL",
                ),
                ToolCapability(
                    name="list_tables", description="List all tables in database"
                ),
                ToolCapability(
                    name="describe_table", description="Get table schema information"
                ),
                ToolCapability(
                    name="bulk_data_export",
                    description="Export large datasets efficiently",
                ),
            ],
            tags=["postgresql", "sql", "database", "query", "ingestion"],
            input_schema=ToolSchema(
                type="object",
                properties={
                    "query": {"type": "string", "description": "SQL query to execute"},
                    "operation": {
                        "type": "string",
                        "enum": ["query", "list_tables", "describe_table"],
                        "description": "Type of database operation",
                    },
                    "table_name": {
                        "type": "string",
                        "description": "Table name (for describe_table operation)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of rows to return",
                    },
                    "parameters": {
                        "type": "array",
                        "description": "Query parameters for parameterized queries",
                    },
                },
                required=["operation"],
                description="PostgreSQL operation parameters",
            ),
            output_schema=ToolSchema(
                type="object",
                properties={
                    "data": {"type": "array", "description": "Query result rows"},
                    "headers": {"type": "array", "description": "Column names"},
                    "row_count": {
                        "type": "integer",
                        "description": "Number of rows returned",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Query execution metadata",
                    },
                },
                description="Database query results",
            ),
            credential_requirements=[
                CredentialRequirement(
                    type=CredentialType.CONNECTION_STRING,
                    provider="postgresql",
                    required=True,
                    description="PostgreSQL connection string",
                )
            ],
        )

    async def _execute_internal(
        self, input_data: Dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        """Execute PostgreSQL operation"""
        try:
            connection_string = self.get_connection_string(context)
            operation = input_data["operation"]

            # Establish connection
            conn = await asyncpg.connect(connection_string)

            try:
                if operation == "query":
                    result_data = await self._execute_query(conn, input_data)
                elif operation == "list_tables":
                    result_data = await self._list_tables(conn)
                elif operation == "describe_table":
                    result_data = await self._describe_table(conn, input_data)
                else:
                    raise ValueError(f"Unsupported operation: {operation}")

                return ExecutionResult(
                    success=True,
                    data=result_data,
                    metadata={"operation": operation, "database": "postgresql"},
                )

            finally:
                await conn.close()

        except Exception as e:
            return ExecutionResult(
                success=False, error_message=f"PostgreSQL operation failed: {str(e)}"
            )

    async def _execute_query(
        self, conn: asyncpg.Connection, input_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute SQL query"""
        query = input_data.get("query")
        if not query:
            raise ValueError("Query is required for query operation")

        parameters = input_data.get("parameters", [])
        limit = input_data.get("limit")

        # Add LIMIT if specified and not already in query
        if limit and "LIMIT" not in query.upper():
            query = f"{query.rstrip(';')} LIMIT {limit}"

        # Execute query
        if parameters:
            rows = await conn.fetch(query, *parameters)
        else:
            rows = await conn.fetch(query)

        if not rows:
            return {
                "data": [],
                "headers": [],
                "row_count": 0,
                "metadata": {"execution_time": 0},
            }

        # Extract headers from first row
        headers = list(rows[0].keys())

        # Convert rows to list of lists
        data = [list(row.values()) for row in rows]

        return {
            "data": data,
            "headers": headers,
            "row_count": len(data),
            "metadata": {"query_executed": query, "parameters_used": bool(parameters)},
        }

    async def _list_tables(self, conn: asyncpg.Connection) -> Dict[str, Any]:
        """List all tables in the database"""
        query = """
        SELECT
            table_name,
            table_type,
            table_schema
        FROM information_schema.tables
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
        ORDER BY table_schema, table_name
        """

        rows = await conn.fetch(query)

        headers = ["table_name", "table_type", "table_schema"]
        data = [list(row.values()) for row in rows]

        return {
            "data": data,
            "headers": headers,
            "row_count": len(data),
            "metadata": {"operation": "list_tables"},
        }

    async def _describe_table(
        self, conn: asyncpg.Connection, input_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Get table schema information"""
        table_name = input_data.get("table_name")
        if not table_name:
            raise ValueError("table_name is required for describe_table operation")

        query = """
        SELECT
            column_name,
            data_type,
            is_nullable,
            column_default,
            character_maximum_length
        FROM information_schema.columns
        WHERE table_name = $1
        ORDER BY ordinal_position
        """

        rows = await conn.fetch(query, table_name)

        if not rows:
            raise ValueError(f"Table '{table_name}' not found")

        headers = [
            "column_name",
            "data_type",
            "is_nullable",
            "column_default",
            "max_length",
        ]
        data = [list(row.values()) for row in rows]

        return {
            "data": data,
            "headers": headers,
            "row_count": len(data),
            "metadata": {"operation": "describe_table", "table_name": table_name},
        }

    def calculate_credits(self, input_data: Any, result: ExecutionResult) -> float:
        """Calculate credits based on data processed"""
        if not result.success or not result.data:
            return Decimal("0.05")  # Minimal charge for failed attempts

        row_count = result.data.get("row_count", 0)
        operation = input_data.get("operation", "query")

        # Different credit rates for different operations
        if operation == "list_tables":
            return Decimal("0.05")  # Fixed low cost
        elif operation == "describe_table":
            return Decimal("0.1")  # Fixed moderate cost
        else:  # query operation
            base_credits = Decimal("0.1")
            row_credits = Decimal(row_count) * Decimal("0.001")
            return base_credits + row_credits

    @classmethod
    def get_ohm_descriptor(cls) -> dict:
        return {
            "kind": "tool",
            "id": "postgresql-reader",
            "version": {"hash": "sha256:postgresql-v1-0-0", "tags": ["1.0.0"]},
            "metadata": {
                "name": "PostgreSQL Reader",
                "description": "Execute queries and read data from PostgreSQL databases",
            },
            "spec": {
                "implementation": {
                    "type": "internal",
                    "handler": "app.tools.implementations.ingestion.postgresql_reader.PostgreSQLReader",
                },
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {"type": "object", "properties": {}},
                "credential_requirements": [
                    {"type": "connection_string", "provider": "postgresql"},
                ],
            },
        }

    @classmethod
    def get_kind(cls) -> DescriptorKind:
        return DescriptorKind.TOOL

    @classmethod
    def get_plugin_id(cls) -> str:
        return "postgresql-reader"


plugin_registry.register(PostgreSQLReader)
tool_registry.register_tool(PostgreSQLReader)
