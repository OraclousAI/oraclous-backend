"""Capability-registry client (ORAA-4 §21 services layer).

The runtime composes the capability-registry over HTTP — it never imports it (four-layer contract).
This client resolves an OHM capability reference to a registry descriptor, materialises a registry
*instance* (the unit the registry executes), configures its credential mappings, and dispatches
operations. Identity is propagated per the trusted-gateway model (ADR-018): the caller passes the
already-built downstream headers (gateway headers + the internal key; dev: a bearer), so
the registry sees the same tenant and its org-scoping holds end-to-end.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx


class RegistryError(Exception):
    """A capability-registry call failed (non-2xx or transport error)."""


def _ref_slug(ref: str) -> str:
    """``core/postgresql-reader@1.0.0`` → ``postgresql-reader`` (the comparable name slug)."""
    tail = ref.split("/")[-1]  # drop core/ or org:<id>/ prefix
    return tail.split("@")[0].strip().lower()


def _name_slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


class RegistryClient:
    def __init__(self, base_url: str, *, headers: dict[str, str], timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Content-Type": "application/json", **headers},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _json(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code // 100 != 2:
            raise RegistryError(
                f"{resp.request.method} {resp.request.url.path} → "
                f"{resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    async def list_tools(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/v1/tools")
        body = await self._json(resp)
        return body.get("capabilities") or []

    async def resolve_capability(
        self, ref: str, *, explicit_id: str | None = None
    ) -> dict[str, Any]:
        """Resolve an OHM capability ``ref`` to a registry tool item (carries ``id`` + a
        ``descriptor``). An explicit ``capability_id`` (from the OHM capability ``config``) wins;
        otherwise match the ref's name slug to a tool's top-level ``name``."""
        tools = await self.list_tools()
        if explicit_id:
            found = next((t for t in tools if str(t.get("id")) == explicit_id), None)
            if found is None:
                raise RegistryError(f"capability_id {explicit_id} not found in the registry")
            return found
        slug = _ref_slug(ref)
        found = next((t for t in tools if _name_slug(t.get("name", "")) == slug), None)
        if found is None:
            raise RegistryError(f"no registry capability matches ref {ref!r} (slug {slug!r})")
        return found

    async def create_instance(
        self, *, capability_id: str, name: str, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/api/v1/instances",
            json={"capability_id": capability_id, "name": name, "configuration": configuration},
        )
        return await self._json(resp)

    async def configure_credentials(
        self, instance_id: uuid.UUID, mappings: dict[str, str]
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"/api/v1/instances/{instance_id}/configure-credentials",
            json={"credential_mappings": mappings},
        )
        return await self._json(resp)

    async def execute(self, instance_id: uuid.UUID, input_data: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(
            f"/api/v1/instances/{instance_id}/execute", json={"input_data": input_data}
        )
        return await self._json(resp)
