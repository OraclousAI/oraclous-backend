from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query

from app.schemas.tool_definition import ToolDefinition, ToolQuery
from app.schemas.common import ToolCategory

router = APIRouter()


@router.post("/", response_model=dict)
async def create_tool(tool: ToolDefinition):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.get("/", response_model=List[ToolDefinition])
async def list_tools(
    category: Optional[ToolCategory] = Query(None),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.get("/{tool_id}", response_model=ToolDefinition)
async def get_tool(tool_id: str):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.put("/{tool_id}", response_model=dict)
async def update_tool(tool_id: str, tool: ToolDefinition):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.delete("/{tool_id}", response_model=dict)
async def delete_tool(tool_id: str):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.post("/search", response_model=List[ToolDefinition])
async def search_tools_advanced(query: ToolQuery):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.post("/match-capabilities", response_model=List[ToolDefinition])
async def match_capabilities(capabilities: List[str]):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.get("/sync-status", response_model=dict)
async def get_tool_sync_status():
    raise HTTPException(status_code=501, detail="Tool sync removed")


@router.post("/resync", response_model=dict)
async def resync_tools():
    raise HTTPException(status_code=501, detail="Tool sync removed")


@router.get("/{tool_id}/availability", response_model=dict)
async def check_tool_availability(tool_id: str):
    raise HTTPException(status_code=501, detail="Use capability registry API")
