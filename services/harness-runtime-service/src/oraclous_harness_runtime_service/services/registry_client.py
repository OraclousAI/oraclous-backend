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

#: The shape a registry ``error_code`` may take. The registry is trusted, but this token is echoed
#: into a string a MODEL reads, so it is accepted only as a bounded lowercase identifier — never as
#: an arbitrary relay channel for whatever happened to land in that field (#483 / ADR-042).
_CODE_TOKEN = re.compile(r"^[a-z0-9_]{1,64}$")

#: What a registry code means, in words the calling member can act on. Owned HERE, not echoed from
#: the upstream body: #692's member was told only "409" for a credential that no longer existed, so
#: it could not tell "reconnect this tool" from "retry later" and simply repeated the call.
_CODE_MEANINGS = {
    "credential_not_found": (
        "the credential connected to this tool no longer exists — it must be reconnected before "
        "this tool can run"
    ),
    "credential_not_mapped": (
        "no credential is connected to this tool — one must be connected before this tool can run"
    ),
    "pending_approval": "this tool is imported but not yet approved by an organisation admin",
    "no_executor": "this tool has no runnable implementation in this deployment",
}


class RegistryError(Exception):
    """A capability-registry call failed (non-2xx or transport error).

    ``error_code`` is the registry's own typed code when it sent one — the single field allowed
    across the leak boundary, because it comes from a closed vocabulary the registry generates and
    never from customer content.
    """

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


def _error_code(resp: httpx.Response) -> str | None:
    """The registry's typed ``error_code``, if the body carries one in the accepted token shape."""
    try:
        body = resp.json()
    except ValueError:  # a proxy's HTML error page, an empty body — nothing typed to read
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("error_code")
    return code if isinstance(code, str) and _CODE_TOKEN.match(code) else None


def _slug(text: str) -> str:
    """Lowercase + collapse every run of non-alphanumerics to a single ``-`` (both sides match)."""
    return _NON_ALNUM.sub("-", text.lower()).strip("-")


def capability_slug(name: str) -> str:
    """The registry row's OWN name, slugified — the only trustworthy identity of a capability.

    A manifest picks its ``binding`` alias freely (``retriever``, ``Read``, anything), so the alias
    never identifies which capability a binding actually resolved to. ``resolve_capability`` proves
    the row's name matches the ref slug, so slugging that name is what a caller may key trust on.
    """
    return _slug(name)


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
            # echo customer input/output) — CLAUDE.md §11 / the ADR-042 leak class. #692: the
            # registry's typed error_code DOES cross, plus this service's own words for it — a bare
            # status told the member nothing it could act on, so it repeated the failing call.
            message = f"{resp.request.method} {resp.request.url.path} → {resp.status_code}"
            code = _error_code(resp)
            if code is not None:
                meaning = _CODE_MEANINGS.get(code)
                message = f"{message} ({code})" + (f": {meaning}" if meaning else "")
            raise RegistryError(message, error_code=code)
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
