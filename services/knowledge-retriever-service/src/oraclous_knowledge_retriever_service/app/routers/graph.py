"""Graph route handlers — traverse and temporal slice (ORAA-60)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from oraclous_knowledge_retriever_service.contracts import NodeResult

router = APIRouter(prefix="/v1/graph", tags=["graph"])


def _stub_result(node_id: str, modality: str) -> NodeResult:
    return NodeResult(
        id=f"{modality}-stub",
        type="node",
        properties={"node_id": node_id, "modality": modality},
    )


@router.get("/traverse")
async def graph_traverse(
    node_id: str = Query(..., description="Starting node identifier"),
) -> list[dict[str, Any]]:
    return [_stub_result(node_id, "traverse")]


@router.get("/temporal")
async def temporal_slice(
    ts: str = Query(..., description="ISO-8601 timestamp for the temporal slice"),
) -> list[dict[str, Any]]:
    return [_stub_result(ts, "temporal")]
