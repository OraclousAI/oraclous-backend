"""Capability-registry client (services layer).

The runtime composes the capability-registry over HTTP — it never imports it (four-layer contract).
This client resolves an OHM capability reference to a registry descriptor, materialises a registry
*instance* (the unit the registry executes), configures its credential mappings, and dispatches
operations. Identity is propagated per the trusted-gateway model (ADR-018): the caller passes the
already-built downstream headers (gateway headers + the internal key; dev: a bearer), so
the registry sees the same tenant and its org-scoping holds end-to-end.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class RegistryError(Exception):
    """A capability-registry call failed (non-2xx or transport error)."""


def _slug(text: str) -> str:
    """Lowercase + collapse every run of non-alphanumerics to a single ``-`` (both sides match)."""
    return _NON_ALNUM.sub("-", text.lower()).strip("-")


def _ref_slug(ref: str) -> str:
    """``core/postgresql-reader@1.0.0`` → ``postgresql-reader`` (drop the prefix + @version)."""
    tail = ref.split("/")[-1].split("@")[0]  # drop core/ or org:<id>/ prefix and @version
    return _slug(tail)


class RegistryClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str],
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Content-Type": "application/json", **headers},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _json(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code // 100 != 2:
            # leak-safe: surface the method/path + coarse status, never the upstream body (it may
            # echo customer input/output) — CLAUDE.md §11 / the ADR-042 leak class.
            raise RegistryError(
                f"{resp.request.method} {resp.request.url.path} → {resp.status_code}"
            )
        return resp.json()

    async def list_tools(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/v1/tools")
        body = await self._json(resp)
        return body.get("capabilities") or []

    async def get_capability(self, capability_id: str) -> dict[str, Any]:
        """Fetch one capability descriptor by id (used to resolve a harness ``manifest_ref``)."""
        return await self._json(await self._client.get(f"/api/v1/capabilities/{capability_id}"))

    async def list_instances(self) -> list[dict[str, Any]]:
        """List the caller-org's tool instances (used to find-or-reuse a harness's instances)."""
        body = await self._json(await self._client.get("/api/v1/instances"))
        return body.get("instances") or []

    async def resolve_capability(
        self, ref: str, *, explicit_id: str | None = None
    ) -> dict[str, Any]:
        """Resolve an OHM capability ``ref`` to a registry tool item (carries ``id`` + descriptor).
        A ``config.capability_id`` selects the row, but its resolved name MUST still match the ref's
        name slug — else a benign ref could smuggle in a different (forbidden) capability by id.
        Without an id, match the ref's name slug to a tool's ``name``. Fail-closed."""
        tools = await self.list_tools()
        slug = _ref_slug(ref)
        if explicit_id:
            found = next((t for t in tools if str(t.get("id")) == explicit_id), None)
            if found is None:
                raise RegistryError(f"capability_id {explicit_id} not found in the registry")
            if _slug(found.get("name", "")) != slug:
                raise RegistryError(
                    f"capability_id {explicit_id} resolves to {found.get('name')!r}, "
                    f"not matching ref {ref!r} (slug {slug!r})"
                )
            return found
        found = next((t for t in tools if _slug(t.get("name", "")) == slug), None)
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
