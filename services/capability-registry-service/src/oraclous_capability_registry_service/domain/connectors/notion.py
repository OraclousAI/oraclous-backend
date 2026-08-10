"""Notion reader connector (domain layer; reshape of legacy
``oraclous-core-service/app/tools/implementations/ingestion/notion_reader.py``).

A real HTTP connector: authenticates to the Notion API with the resolved ``api_key`` and runs read
operations (``search``, ``read_page``). ``read_page`` returns the page BODY under ``content`` —
fetched from ``/v1/blocks/{page_id}/children`` (paginated), because ``/v1/pages/{page_id}`` only
carries the page's properties — plus a connector-authored ``SourceRef`` under ``source`` (#770).
The live API call is key-gated — a real Notion integration token is required for a successful call;
the resolution + dispatch seam is exercised key-free via the fake broker (and unit-tested with a
mocked transport). ``transport`` is an injectable test seam.
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
                if resp.status_code != 200:
                    return _api_error(resp)
                return ExecutionResult(success=True, data={"documents": resp.json()})
            if operation == "read_page":
                page_id = input_data.get("page_id")
                if not page_id:
                    return ExecutionResult(
                        success=False,
                        error_message="'page_id' is required for read_page",
                        error_type="INVALID_INPUT",
                    )
                return await self._read_page(client, str(page_id))
            return ExecutionResult(
                success=False,
                error_message=f"unsupported operation '{operation}'",
                error_type="INVALID_OPERATION",
            )

    async def _read_page(self, client: httpx.AsyncClient, page_id: str) -> ExecutionResult:
        page_resp = await client.get(f"/v1/pages/{page_id}")
        if page_resp.status_code != 200:
            return _api_error(page_resp)
        blocks: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"start_cursor": cursor} if cursor else {}
            blocks_resp = await client.get(f"/v1/blocks/{page_id}/children", params=params)
            if blocks_resp.status_code != 200:
                return _api_error(blocks_resp)
            payload = blocks_resp.json()
            blocks.extend(payload.get("results") or [])
            cursor = payload.get("next_cursor")
            if not payload.get("has_more") or not cursor:
                break
        return ExecutionResult(
            success=True,
            data={
                "content": _block_text(blocks),
                "source": _source_ref(page_id=page_id, page=page_resp.json()),
            },
        )


def _api_error(resp: httpx.Response) -> ExecutionResult:
    return ExecutionResult(
        success=False,
        error_message=f"Notion API returned {resp.status_code}",
        error_type="NOTION_API_ERROR",
        metadata={"status_code": resp.status_code},
    )


def _block_text(blocks: list[dict[str, Any]]) -> str:
    """The blocks' ``rich_text`` plain text, one line per block, in block order."""
    lines: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        payload = block.get(block_type) if isinstance(block_type, str) else None
        if not isinstance(payload, dict):
            continue
        line = "".join(str(rt.get("plain_text") or "") for rt in payload.get("rich_text") or [])
        if line:
            lines.append(line)
    return "\n".join(lines)


def _source_ref(*, page_id: str, page: dict[str, Any]) -> dict[str, Any]:
    """The page's source identity, from the page object already fetched (§CITE).

    Connector-authored from source-native fields only, mirroring the GitHub reader's
    ``_source_ref``. It never mints: ``citation_id`` and ``retrieved_at`` are the platform's,
    computed at the tool-execution boundary. ``revision`` is the page's ``last_edited_time`` and
    travels with its kind (``edited_at``) or not at all. ``author`` is null because the page
    object carries only opaque user ids — resolving a display name costs a second API call, so
    the gap is recorded honestly rather than inventing one. ``permission_ref`` is carried and
    unenforced, same as GitHub.
    """
    last_edited = page.get("last_edited_time")
    return {
        "source_system": "notion",
        "source_id": page_id,
        "revision": last_edited,
        "revision_kind": "edited_at" if last_edited else None,
        "url": page.get("url"),
        "title": _title(page),
        "author": None,
        "permission_ref": None,
    }


def _title(page: dict[str, Any]) -> str | None:
    """The title property's concatenated plain text, or None when the page carries none."""
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            text = "".join(str(rt.get("plain_text") or "") for rt in prop.get("title") or [])
            return text or None
    return None
