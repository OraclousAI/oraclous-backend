"""Search route handlers — semantic, fulltext, hybrid (ORAA-60)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from oraclous_knowledge_retriever_service.contracts import NodeResult

router = APIRouter(prefix="/v1/search", tags=["search"])


class SearchRequest(BaseModel):
    query: str


def _stub_result(query: str, modality: str) -> NodeResult:
    return NodeResult(
        id=f"{modality}-stub",
        type="node",
        properties={"query": query, "modality": modality},
    )


@router.post("/semantic")
async def semantic_search(req: SearchRequest) -> list[dict[str, Any]]:
    return [_stub_result(req.query, "semantic")]


@router.post("/fulltext")
async def fulltext_search(req: SearchRequest) -> list[dict[str, Any]]:
    return [_stub_result(req.query, "fulltext")]


@router.post("/hybrid")
async def hybrid_search(req: SearchRequest) -> list[dict[str, Any]]:
    return [_stub_result(req.query, "hybrid")]
