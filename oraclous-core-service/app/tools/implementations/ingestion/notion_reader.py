# app/tools/implementations/ingestion/notion_reader.py
from typing import Any, Dict, List
from datetime import datetime
from decimal import Decimal

from app.utils.tool_id_generator import generate_tool_id
from app.tools.base.internal_tool import InternalTool
from app.tools.plugin import CapabilityKindPlugin, plugin_registry
from app.tools.registry import tool_registry
from app.models.capability_descriptor import DescriptorKind
from app.schemas.tool_instance import ExecutionContext, ExecutionResult
from app.schemas.tool_definition import (
    ToolDefinition,
    ToolSchema,
    ToolCapability,
    CredentialRequirement,
)
from app.schemas.common import ToolCategory, ToolType, CredentialType


class NotionReader(InternalTool, CapabilityKindPlugin):
    """
    Tool for reading data from Notion databases and pages
    """

    NOTION_API_BASE = "https://api.notion.com/v1"
    NOTION_VERSION = "2022-06-28"

    def __init__(self, definition: ToolDefinition):
        super().__init__(definition)

    @classmethod
    def get_tool_definition(cls) -> ToolDefinition:
        """Return the tool definition for Notion Reader"""
        return ToolDefinition(
            id=generate_tool_id("Notion Reader", "1.0.0", "INGESTION"),
            name="Notion Reader",
            description="Read and extract data from Notion databases and pages",
            version="1.0.0",
            category=ToolCategory.INGESTION,
            type=ToolType.INTERNAL,
            capabilities=[
                ToolCapability(
                    name="read_notion_database",
                    description="Read data from Notion databases",
                ),
                ToolCapability(
                    name="read_notion_page",
                    description="Read content from Notion pages",
                ),
                ToolCapability(
                    name="query_notion_database",
                    description="Query Notion databases with filters",
                ),
                ToolCapability(
                    name="extract_structured_data",
                    description="Extract structured data from Notion properties",
                ),
            ],
            tags=["notion", "database", "page", "productivity", "ingestion"],
            input_schema=ToolSchema(
                type="object",
                properties={
                    "database_id": {
                        "type": "string",
                        "description": "Notion database ID (for database queries)",
                    },
                    "page_id": {
                        "type": "string",
                        "description": "Notion page ID (for page content)",
                    },
                    "query_type": {
                        "type": "string",
                        "enum": ["database", "page"],
                        "description": "Type of Notion object to query",
                    },
                    "filters": {
                        "type": "object",
                        "description": "Notion database query filters",
                    },
                    "sorts": {
                        "type": "array",
                        "description": "Sorting criteria for database queries",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Number of results per page (max 100)",
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "extract_content": {
                        "type": "boolean",
                        "description": "Whether to extract page content (for pages)",
                    },
                },
                required=["query_type"],
                description="Input parameters for Notion data extraction",
            ),
            output_schema=ToolSchema(
                type="object",
                properties={
                    "data": {
                        "type": "array",
                        "description": "Extracted data rows/pages",
                    },
                    "headers": {
                        "type": "array",
                        "description": "Database property names or page structure",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Query metadata and pagination info",
                    },
                    "row_count": {
                        "type": "integer",
                        "description": "Number of results returned",
                    },
                },
                description="Extracted Notion data and metadata",
            ),
            credential_requirements=[
                CredentialRequirement(
                    type=CredentialType.API_KEY,
                    provider="notion",
                    required=True,
                    description="Notion Internal Integration Token",
                )
            ],
        )

    async def _execute_internal(
        self, input_data: Dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        """Execute Notion data reading"""
        try:
            # Get API key
            api_creds = self.get_credentials(context, "API_KEY")
            api_key = api_creds["api_key"]

            query_type = input_data["query_type"]

            if query_type == "database":
                result_data = await self._query_database(input_data, api_key)
            elif query_type == "page":
                result_data = await self._read_page(input_data, api_key)
            else:
                raise ValueError(f"Unsupported query type: {query_type}")

            return ExecutionResult(
                success=True,
                data=result_data,
                metadata={
                    "query_type": query_type,
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )

        except Exception as e:
            return ExecutionResult(
                success=False, error_message=f"Failed to read Notion data: {str(e)}"
            )

    async def _query_database(
        self, input_data: Dict[str, Any], api_key: str
    ) -> Dict[str, Any]:
        """Query Notion database"""
        database_id = input_data.get("database_id")
        if not database_id:
            raise ValueError("database_id is required for database queries")

        # Prepare query payload
        query_payload = {"page_size": input_data.get("page_size", 100)}

        if "filters" in input_data:
            query_payload["filter"] = input_data["filters"]

        if "sorts" in input_data:
            query_payload["sorts"] = input_data["sorts"]

        # Make API request
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": self.NOTION_VERSION,
            "Content-Type": "application/json",
        }

        url = f"{self.NOTION_API_BASE}/databases/{database_id}/query"

        async with self.http_client as client:
            response = await client.post(url, headers=headers, json=query_payload)

            if response.status_code != 200:
                raise Exception(
                    f"Notion API error: {response.status_code} - {response.text}"
                )

            response_data = response.json()

        # Process results
        results = response_data.get("results", [])

        if not results:
            return {
                "data": [],
                "headers": [],
                "row_count": 0,
                "metadata": {"has_more": False},
            }

        # Extract property names (headers) from first result
        first_result = results[0]
        headers = list(first_result.get("properties", {}).keys())

        # Extract data rows
        data = []
        for result in results:
            row = []
            properties = result.get("properties", {})

            for header in headers:
                prop = properties.get(header, {})
                value = self._extract_property_value(prop)
                row.append(value)

            data.append(row)

        return {
            "data": data,
            "headers": headers,
            "row_count": len(data),
            "metadata": {
                "has_more": response_data.get("has_more", False),
                "next_cursor": response_data.get("next_cursor"),
            },
        }

    async def _read_page(
        self, input_data: Dict[str, Any], api_key: str
    ) -> Dict[str, Any]:
        """Read Notion page content"""
        page_id = input_data.get("page_id")
        if not page_id:
            raise ValueError("page_id is required for page queries")

        extract_content = input_data.get("extract_content", True)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": self.NOTION_VERSION,
        }

        # Get page properties
        page_url = f"{self.NOTION_API_BASE}/pages/{page_id}"

        async with self.http_client as client:
            response = await client.get(page_url, headers=headers)

            if response.status_code != 200:
                raise Exception(
                    f"Notion API error: {response.status_code} - {response.text}"
                )

            page_data = response.json()

        result = {
            "data": [],
            "headers": ["property", "value"],
            "row_count": 0,
            "metadata": {
                "page_id": page_id,
                "created_time": page_data.get("created_time"),
                "last_edited_time": page_data.get("last_edited_time"),
            },
        }

        # Extract page properties
        properties = page_data.get("properties", {})
        data_rows = []

        for prop_name, prop_data in properties.items():
            value = self._extract_property_value(prop_data)
            data_rows.append([prop_name, value])

        # Extract page content if requested
        if extract_content:
            content_data = await self._extract_page_content(page_id, api_key, headers)
            if content_data:
                data_rows.append(["page_content", content_data])

        result["data"] = data_rows
        result["row_count"] = len(data_rows)

        return result

    async def _extract_page_content(
        self, page_id: str, api_key: str, headers: Dict[str, str]
    ) -> str:
        """Extract text content from page blocks"""
        blocks_url = f"{self.NOTION_API_BASE}/blocks/{page_id}/children"

        async with self.http_client as client:
            response = await client.get(blocks_url, headers=headers)

            if response.status_code != 200:
                return ""

            blocks_data = response.json()

        content_parts = []

        for block in blocks_data.get("results", []):
            block_type = block.get("type")

            if block_type in [
                "paragraph",
                "heading_1",
                "heading_2",
                "heading_3",
                "bulleted_list_item",
                "numbered_list_item",
            ]:
                text_content = self._extract_text_from_block(block.get(block_type, {}))
                if text_content:
                    content_parts.append(text_content)

        return "\n".join(content_parts)

    def _extract_text_from_block(self, block_data: Dict[str, Any]) -> str:
        """Extract text from a block object"""
        rich_text = block_data.get("rich_text", [])
        text_parts = []

        for text_obj in rich_text:
            if text_obj.get("type") == "text":
                text_parts.append(text_obj.get("text", {}).get("content", ""))

        return "".join(text_parts)

    def _extract_property_value(self, property_data: Dict[str, Any]) -> Any:
        """Extract value from Notion property object"""
        prop_type = property_data.get("type")

        if not prop_type:
            return None

        prop_content = property_data.get(prop_type)

        if prop_type == "title":
            return self._extract_rich_text(prop_content)
        elif prop_type == "rich_text":
            return self._extract_rich_text(prop_content)
        elif prop_type == "number":
            return prop_content
        elif prop_type == "select":
            return prop_content.get("name") if prop_content else None
        elif prop_type == "multi_select":
            return [item.get("name") for item in prop_content] if prop_content else []
        elif prop_type == "date":
            if prop_content:
                date_obj = {
                    "start": prop_content.get("start"),
                    "end": prop_content.get("end"),
                    "time_zone": prop_content.get("time_zone"),
                }
                return date_obj if date_obj["start"] else None
            return None
        elif prop_type == "checkbox":
            return prop_content
        elif prop_type == "url":
            return prop_content
        elif prop_type == "email":
            return prop_content
        elif prop_type == "phone_number":
            return prop_content
        elif prop_type == "formula":
            # Extract the computed value from formula
            if prop_content and prop_content.get("type"):
                formula_type = prop_content.get("type")
                return prop_content.get(formula_type)
            return None
        elif prop_type == "relation":
            # Return list of related page IDs
            return [rel.get("id") for rel in prop_content] if prop_content else []
        elif prop_type == "rollup":
            # Extract rollup values
            if prop_content and prop_content.get("type"):
                rollup_type = prop_content.get("type")
                if rollup_type == "array":
                    return [item for item in prop_content.get("array", [])]
                else:
                    return prop_content.get(rollup_type)
            return None
        elif prop_type == "people":
            # Return list of people (users)
            return (
                [person.get("name", person.get("id")) for person in prop_content]
                if prop_content
                else []
            )
        elif prop_type == "files":
            # Return list of file information
            files = []
            for file_obj in prop_content or []:
                file_info = {"name": file_obj.get("name"), "type": file_obj.get("type")}
                if file_obj.get("type") == "external":
                    file_info["url"] = file_obj.get("external", {}).get("url")
                elif file_obj.get("type") == "file":
                    file_info["url"] = file_obj.get("file", {}).get("url")
                files.append(file_info)
            return files
        elif prop_type == "created_time":
            return prop_content
        elif prop_type == "created_by":
            return (
                prop_content.get("name", prop_content.get("id"))
                if prop_content
                else None
            )
        elif prop_type == "last_edited_time":
            return prop_content
        elif prop_type == "last_edited_by":
            return (
                prop_content.get("name", prop_content.get("id"))
                if prop_content
                else None
            )
        elif prop_type == "status":
            return prop_content.get("name") if prop_content else None
        else:
            # For unknown property types, return the raw content
            return prop_content

    def _extract_rich_text(self, rich_text_array: List[Dict[str, Any]]) -> str:
        """Extract plain text from rich text array"""
        if not rich_text_array:
            return ""

        text_parts = []
        for text_obj in rich_text_array:
            if text_obj.get("type") == "text":
                text_parts.append(text_obj.get("text", {}).get("content", ""))
            elif text_obj.get("type") == "mention":
                # Handle mentions
                mention = text_obj.get("mention", {})
                if mention.get("type") == "page":
                    text_parts.append(text_obj.get("plain_text", ""))
                elif mention.get("type") == "user":
                    text_parts.append(text_obj.get("plain_text", ""))
                else:
                    text_parts.append(text_obj.get("plain_text", ""))
            elif text_obj.get("type") == "equation":
                text_parts.append(text_obj.get("equation", {}).get("expression", ""))

        return "".join(text_parts)

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.http_client.aclose()

    async def close(self):
        """Close the HTTP client"""
        await self.http_client.aclose()

    def calculate_credits(self, input_data: Any, result: ExecutionResult) -> Decimal:
        if not result.success or not result.data:
            return Decimal("0.1")

        row_count = result.data.get("row_count", 0)
        base_credits = Decimal("0.1")
        row_credits = Decimal(str(row_count)) * Decimal("0.001")

        return base_credits + row_credits

    @classmethod
    def get_ohm_descriptor(cls) -> dict:
        return {
            "kind": "tool",
            "id": "notion-reader",
            "version": {"hash": "sha256:notion-v1-0-0", "tags": ["1.0.0"]},
            "metadata": {
                "name": "Notion Reader",
                "description": "Read and extract data from Notion databases and pages",
            },
            "spec": {
                "implementation": {
                    "type": "internal",
                    "handler": "app.tools.implementations.ingestion.notion_reader.NotionReader",
                },
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {"type": "object", "properties": {}},
                "credential_requirements": [
                    {"type": "api_key", "required": True, "provider": "notion"},
                ],
            },
        }

    @classmethod
    def get_kind(cls) -> DescriptorKind:
        return DescriptorKind.TOOL

    @classmethod
    def get_plugin_id(cls) -> str:
        return "notion-reader"


plugin_registry.register(NotionReader)
tool_registry.register_tool(NotionReader)
