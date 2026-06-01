from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.services.capability_registry import CapabilityRegistryService
from app.schemas.tool_definition import ToolDefinition, ToolQuery
from app.schemas.common import ToolCategory

router = APIRouter()


async def get_capability_registry(
    db: AsyncSession = Depends(get_session),
) -> CapabilityRegistryService:
    return CapabilityRegistryService(db)


@router.post("/", response_model=dict)
async def create_tool(
    tool: ToolDefinition, registry: CapabilityRegistryService = Depends(get_capability_registry)
):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.get("/", response_model=List[ToolDefinition])
async def list_tools(
    category: Optional[ToolCategory] = Query(None),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    registry: CapabilityRegistryService = Depends(get_capability_registry),
):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.get("/{tool_id}", response_model=ToolDefinition)
async def get_tool(
    tool_id: str, registry: CapabilityRegistryService = Depends(get_capability_registry)
):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.put("/{tool_id}", response_model=dict)
async def update_tool(
    tool_id: str,
    tool: ToolDefinition,
    registry: CapabilityRegistryService = Depends(get_capability_registry),
):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.delete("/{tool_id}", response_model=dict)
async def delete_tool(
    tool_id: str, registry: CapabilityRegistryService = Depends(get_capability_registry)
):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.post("/search", response_model=List[ToolDefinition])
async def search_tools_advanced(
    query: ToolQuery, registry: CapabilityRegistryService = Depends(get_capability_registry)
):
    raise HTTPException(status_code=501, detail="Use capability registry API")


@router.post("/match-capabilities", response_model=List[ToolDefinition])
async def match_capabilities(
    capabilities: List[str], registry: CapabilityRegistryService = Depends(get_capability_registry)
):
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
