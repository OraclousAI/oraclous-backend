from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.services.tool_registry import ToolRegistryService
from app.schemas.tool_definition import ToolDefinition, ToolQuery
from app.schemas.common import ToolCategory

router = APIRouter()


async def get_tool_registry(
    db: AsyncSession = Depends(get_session),
) -> ToolRegistryService:
    return ToolRegistryService(db)


@router.post("/", response_model=dict)
async def create_tool(
    tool: ToolDefinition, registry: ToolRegistryService = Depends(get_tool_registry)
):
    """Register a new tool definition"""
    success = await registry.register_tool(tool)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to register tool")

    return {"message": "Tool registered successfully", "tool_id": tool.id}


@router.get("/", response_model=List[ToolDefinition])
async def list_tools(
    category: Optional[ToolCategory] = Query(None),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    registry: ToolRegistryService = Depends(get_tool_registry),
):
    """List tools with optional filtering"""
    return await registry.list_tools(category=category, limit=limit, offset=offset)


@router.get("/{tool_id}", response_model=ToolDefinition)
async def get_tool(
    tool_id: str, registry: ToolRegistryService = Depends(get_tool_registry)
):
    """Get a specific tool definition"""
    tool = await registry.get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    return tool


@router.put("/{tool_id}", response_model=dict)
async def update_tool(
    tool_id: str,
    tool: ToolDefinition,
    registry: ToolRegistryService = Depends(get_tool_registry),
):
    """Update a tool definition"""
    success = await registry.update_tool(tool_id, tool)
    if not success:
        raise HTTPException(status_code=404, detail="Tool not found or update failed")

    return {"message": "Tool updated successfully"}


@router.delete("/{tool_id}", response_model=dict)
async def delete_tool(
    tool_id: str, registry: ToolRegistryService = Depends(get_tool_registry)
):
    """Delete a tool definition"""
    success = await registry.delete_tool(tool_id)
    if not success:
        raise HTTPException(status_code=404, detail="Tool not found")

    return {"message": "Tool deleted successfully"}


@router.post("/search", response_model=List[ToolDefinition])
async def search_tools_advanced(
    query: ToolQuery, registry: ToolRegistryService = Depends(get_tool_registry)
):
    """Advanced search using ToolQuery model"""
    return await registry.search_tools_advanced(query)


@router.post("/match-capabilities", response_model=List[ToolDefinition])
async def match_capabilities(
    capabilities: List[str], registry: ToolRegistryService = Depends(get_tool_registry)
):
    """Find tools that match required capabilities"""
    return await registry.match_capabilities(capabilities)


