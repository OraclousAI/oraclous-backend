"""Internal schema-lookup endpoint (ORAA-57).

Service-to-service only — no bearer-token auth.
Route: GET /internal/v1/schema/{graph_id}
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/schema/{graph_id}")
async def get_schema(graph_id: str) -> dict:
    import oraclous_knowledge_graph_service.api.schema as _schema_mod  # ORA-48

    try:
        result = await _schema_mod.schema_manager.extract_schema(graph_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Graph schema not found") from None
    return result.model_dump(mode="json")
