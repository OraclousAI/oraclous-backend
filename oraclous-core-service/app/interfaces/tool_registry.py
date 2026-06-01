from abc import ABC, abstractmethod
from typing import Optional, List
from app.schemas.tool_definition import ToolDefinition
from app.schemas.common import ToolCategory


class BaseToolRegistry(ABC):
    """
    Abstract base class for tool registry operations
    """

    @abstractmethod
    async def register_tool(self, definition: ToolDefinition) -> bool:
        """Register a new tool definition"""
        pass

    @abstractmethod
    async def get_tool(self, tool_id: str) -> Optional[ToolDefinition]:
        """Retrieve tool definition by ID"""
        pass

    @abstractmethod
    async def search_tools(
        self,
        query: str,
        category: Optional[ToolCategory] = None,
        capabilities: Optional[List[str]] = None,
    ) -> List[ToolDefinition]:
        """Search for tools by query and filters"""
        pass

    @abstractmethod
    async def list_tools(
        self,
        category: Optional[ToolCategory] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ToolDefinition]:
        """List tools with pagination"""
        pass

    @abstractmethod
    async def update_tool(self, tool_id: str, definition: ToolDefinition) -> bool:
        """Update an existing tool definition"""
        pass

    @abstractmethod
    async def delete_tool(self, tool_id: str) -> bool:
        """Delete a tool definition"""
        pass

    @abstractmethod
    async def match_capabilities(
        self, required_capabilities: List[str]
    ) -> List[ToolDefinition]:
        """Find tools that match required capabilities"""
        pass
