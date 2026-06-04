"""Internal schema route (ORAA-4 §21 routes layer).

GET /internal/v1/schema/{graph_id} returns the org-scoped label/relationship shape of a graph. The
read is filtered by organisation_id AND graph_id (bound params) — a caller from another org sees an
empty schema. The sync Neo4j read runs off the event loop via a worker thread.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter

from oraclous_knowledge_graph_service.core.dependencies import GraphWriteRepoDep, UserIdDep
from oraclous_knowledge_graph_service.schema.ingest_schemas import (
    LabelCount,
    RelTypeCount,
    SchemaResponse,
)

router = APIRouter(prefix="/internal/v1", tags=["internal"])


@router.get("/schema/{graph_id}", response_model=SchemaResponse)
async def get_graph_schema(
    graph_id: uuid.UUID, repo: GraphWriteRepoDep, _user_id: UserIdDep
) -> SchemaResponse:
    from oraclous_substrate.access import enforced_organisation_id

    organisation_id = enforced_organisation_id()
    data = await asyncio.to_thread(
        repo.schema, graph_id=str(graph_id), organisation_id=organisation_id
    )
    return SchemaResponse(
        graph_id=graph_id,
        labels=[LabelCount(label=row["label"], count=row["count"]) for row in data["labels"]],
        relationships=[
            RelTypeCount(type=row["type"], count=row["count"]) for row in data["relationships"]
        ],
    )
