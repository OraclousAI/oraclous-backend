import time
from typing import Dict, List, Optional, Tuple

from app.interfaces.tool_registry import BaseToolRegistry
from app.schemas.common import ToolCategory
from app.schemas.tool_definition import ToolDefinition


class CachingToolRegistry(BaseToolRegistry):
    """Read-through in-memory cache wrapping any BaseToolRegistry backing store."""

    def __init__(self, backing: BaseToolRegistry, ttl_seconds: int = 300) -> None:
        self._backing = backing
        self._ttl = ttl_seconds
        # (value, expiry_monotonic) keyed by tool_id
        self._get_cache: Dict[str, Tuple[Optional[ToolDefinition], float]] = {}
        # (value, expiry_monotonic) keyed by (category, limit, offset)
        self._list_cache: Dict[Tuple, Tuple[List[ToolDefinition], float]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _expiry(self) -> float:
        return time.monotonic() + self._ttl

    def _is_expired(self, expiry: float) -> bool:
        return time.monotonic() >= expiry

    def _invalidate_get(self, tool_id: str) -> None:
        self._get_cache.pop(tool_id, None)

    def _invalidate_list(self) -> None:
        self._list_cache.clear()

    # ------------------------------------------------------------------
    # BaseToolRegistry interface
    # ------------------------------------------------------------------

    async def get_tool(self, tool_id: str) -> Optional[ToolDefinition]:
        entry = self._get_cache.get(tool_id)
        if entry is not None:
            value, expiry = entry
            if not self._is_expired(expiry):
                return value
        result = await self._backing.get_tool(tool_id)
        self._get_cache[tool_id] = (result, self._expiry())
        return result

    async def list_tools(
        self,
        category: Optional[ToolCategory] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ToolDefinition]:
        key = (category, limit, offset)
        entry = self._list_cache.get(key)
        if entry is not None:
            value, expiry = entry
            if not self._is_expired(expiry):
                return value
        result = await self._backing.list_tools(
            category=category, limit=limit, offset=offset
        )
        self._list_cache[key] = (result, self._expiry())
        return result

    async def register_tool(self, definition: ToolDefinition) -> bool:
        result = await self._backing.register_tool(definition)
        self._invalidate_get(str(definition.id))
        self._invalidate_list()
        return result

    async def update_tool(self, tool_id: str, definition: ToolDefinition) -> bool:
        result = await self._backing.update_tool(tool_id, definition)
        self._invalidate_get(tool_id)
        self._invalidate_list()
        return result

    async def delete_tool(self, tool_id: str) -> bool:
        result = await self._backing.delete_tool(tool_id)
        self._invalidate_get(tool_id)
        self._invalidate_list()
        return result

    async def search_tools(
        self,
        query: str,
        category: Optional[ToolCategory] = None,
        capabilities: Optional[List[str]] = None,
    ) -> List[ToolDefinition]:
        return await self._backing.search_tools(
            query=query, category=category, capabilities=capabilities
        )

    async def match_capabilities(
        self, required_capabilities: List[str]
    ) -> List[ToolDefinition]:
        return await self._backing.match_capabilities(required_capabilities)
