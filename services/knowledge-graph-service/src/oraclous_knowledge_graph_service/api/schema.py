"""Schema models and schema-manager singleton for knowledge-graph-service (ORAA-55).

``GraphSchema`` and ``NodeSchema`` are Pydantic models returned by the schema
endpoints. ``schema_manager`` is a module-level singleton patched by the test
suite at:
  oraclous_knowledge_graph_service.api.schema.schema_manager
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class NodeSchema(BaseModel):
    label: str
    properties: dict[str, str]
    sample_count: int
    indexes: list[str]


class GraphSchema(BaseModel):
    graph_id: str
    nodes: dict[str, NodeSchema]
    relationships: dict[str, object]
    constraints: list[object]
    indexes: list[object]
    last_updated: datetime
    schema_version: str


class _SchemaManager:
    """Stub schema manager. Real implementation queries Neo4j for schema info."""

    async def extract_schema(self, graph_id: str) -> GraphSchema:
        raise NotImplementedError("Schema extraction not yet implemented")


schema_manager = _SchemaManager()
