"""GitHub reader connector (domain layer).

A real HTTP connector: authenticates to the GitHub REST API with the resolved ``api_key`` (a PAT)
and runs read operations (``list_files``, ``read_file``). The live call is key-gated;
the resolution + dispatch seam is exercised key-free via the fake broker (and unit-tested with a
mocked transport). ``transport`` is an injectable test seam.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

_GITHUB_BASE = "https://api.github.com"


class GitHubReader(InternalTool):
    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    def _token(self, context: ExecutionContext) -> str:
        creds = self.get_credentials(context, "api_key")
        if not creds or not creds.get("api_key"):
            raise ValueError("api_key credential not found in execution context")
        return str(creds["api_key"])

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        token = self._token(context)
        operation = input_data.get("operation", "list_files")
        repo = input_data.get("repo")
        if not repo:
            return ExecutionResult(
                success=False, error_message="'repo' is required", error_type="INVALID_INPUT"
            )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        path = input_data.get("path", "")
        async with httpx.AsyncClient(
            base_url=_GITHUB_BASE, headers=headers, timeout=30.0, transport=self.transport
        ) as client:
            if operation in ("list_files", "read_file"):
                resp = await client.get(f"/repos/{repo}/contents/{path}")
            else:
                return ExecutionResult(
                    success=False,
                    error_message=f"unsupported operation '{operation}'",
                    error_type="INVALID_OPERATION",
                )
        if resp.status_code != 200:
            return ExecutionResult(
                success=False,
                error_message=f"GitHub API returned {resp.status_code}",
                error_type="GITHUB_API_ERROR",
                metadata={"status_code": resp.status_code},
            )
        payload = resp.json()
        if operation == "read_file" and isinstance(payload, dict) and payload.get("content"):
            decoded = base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
            return ExecutionResult(
                success=True,
                data={
                    "path": payload.get("path"),
                    "content": decoded,
                    "source": _source_ref(repo=str(repo), payload=payload),
                },
            )
        return ExecutionResult(success=True, data={"entries": payload})


def _source_ref(*, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The source identity of the file just read, from the response already parsed.

    No second network call: the Contents API response carries ``sha`` and ``html_url``, and both
    were previously discarded — which is why a knowledge record ended up with an untyped
    ``ingestion_source`` and no way to resolve it.

    The connector is the one party holding source-native identity, so it is the one party that may
    fill this. It never mints: ``citation_id`` and ``retrieved_at`` are the platform's, computed at
    the tool-execution boundary.

    ``author`` is null because the Contents API response carries none. Fetching one costs a second
    API call per read and belongs with the source object, so this records the gap honestly rather
    than inventing a name. ``permission_ref`` is carried and unenforced, so records written today
    stay usable when per-user permission mirroring lands.
    """
    path = str(payload.get("path") or "")
    return {
        "source_system": "github",
        "source_id": f"{repo}:{path}",
        "revision": payload.get("sha"),
        "revision_kind": "blob_sha",
        "url": payload.get("html_url"),
        "title": path.rsplit("/", 1)[-1],
        "author": None,
        "permission_ref": None,
    }
