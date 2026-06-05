"""Notion reader connector (ORAA-4 §21 domain layer; reshape of legacy
``oraclous-core-service/app/tools/implementations/ingestion/notion_reader.py``).

A real HTTP connector: authenticates to the Notion API with the resolved ``api_key`` and runs read
operations (``search``, ``read_page``). The live API call is key-gated — a real Notion integration
token is required for a successful call; the resolution + dispatch seam is exercised key-free via
the fake broker (and unit-tested with a mocked transport). ``transport`` is an injectable test seam.
"""

from __future__ import annotations

from typing import Any

import httpx

from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

_NOTION_BASE = "https://api.notion.com"
_NOTION_VERSION = "2022-06-28"


class NotionReader(InternalTool):
    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    def _api_key(self, context: ExecutionContext) -> str:
        creds = self.get_credentials(context, "api_key")
        if not creds or not creds.get("api_key"):
            raise ValueError("api_key credential not found in execution context")
        return str(creds["api_key"])

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        api_key = self._api_key(context)
        operation = input_data.get("operation", "search")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            base_url=_NOTION_BASE, headers=headers, timeout=30.0, transport=self.transport
        ) as client:
            if operation == "search":
                resp = await client.post("/v1/search", json={"query": input_data.get("query", "")})
            elif operation == "read_page":
                page_id = input_data.get("page_id")
                if not page_id:
                    return ExecutionResult(
                        success=False,
                        error_message="'page_id' is required for read_page",
                        error_type="INVALID_INPUT",
                    )
                resp = await client.get(f"/v1/pages/{page_id}")
            else:
                return ExecutionResult(
                    success=False,
                    error_message=f"unsupported operation '{operation}'",
                    error_type="INVALID_OPERATION",
                )
        if resp.status_code != 200:
            return ExecutionResult(
                success=False,
                error_message=f"Notion API returned {resp.status_code}",
                error_type="NOTION_API_ERROR",
                metadata={"status_code": resp.status_code},
            )
        return ExecutionResult(success=True, data={"documents": resp.json()})
