from typing import Dict, Type, List
from app.interfaces.tool_executor import BaseToolExecutor
from app.schemas.tool_definition import ToolDefinition


class _ToolExecutorRegistry:
    def __init__(self):
        self._definitions: Dict[str, ToolDefinition] = {}
        self._executors: Dict[str, Type[BaseToolExecutor]] = {}

    def register_tool(
        self, tool_class: Type[BaseToolExecutor], definition: ToolDefinition = None
    ):
        if definition is None:
            if hasattr(tool_class, "get_tool_definition"):
                definition = tool_class.get_tool_definition()
            else:
                raise ValueError(
                    f"Tool class {tool_class.__name__} must provide definition"
                )
        self._definitions[definition.id] = definition
        self._executors[definition.id] = tool_class

    def get_definition(self, tool_id: str) -> ToolDefinition:
        return self._definitions.get(tool_id)

    def get_executor_class(self, tool_id: str) -> Type[BaseToolExecutor]:
        return self._executors.get(tool_id)

    def list_definitions(self) -> List[ToolDefinition]:
        return list(self._definitions.values())

    def create_executor(self, tool_id: str) -> BaseToolExecutor:
        executor_class = self.get_executor_class(tool_id)
        if not executor_class:
            raise ValueError(f"No executor found for tool: {tool_id}")
        definition = self.get_definition(tool_id)
        return executor_class(definition)


tool_registry = _ToolExecutorRegistry()
