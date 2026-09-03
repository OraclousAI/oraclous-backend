"""Capability-registry client (services layer).

The engine composes the capability-registry over HTTP — it never imports it (four-layer contract:
the engine is Layer 3, the registry Layer 2, so they talk by API exactly as the harness calls the
registry). Identity is propagated per the trusted-gateway model (ADR-018): the caller passes the
already-built downstream headers (gateway headers + the internal key; dev: a bearer), so the
registry sees the same tenant as the schedule owner that fired the run (#489).

The engine-side names (``RegistryClientError``/``RegistryRejected``) deliberately differ from any
registry-side ``RegistryError`` so there is no cross-service collision — this is the engine's own,
self-contained HTTP client.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx


class RegistryClientError(Exception):
    """A capability-registry call failed — the BASE/transport case: the registry was unreachable (a
    connect/pool error). A reachable-but-rejecting registry raises ``RegistryRejected`` instead, so
    the engine can tell a transport failure apart from a rejection (mirrors the harness client)."""


class RegistryRejected(RegistryClientError):
    """The registry WAS reachable and answered with a non-2xx response (e.g. a 422 input-validation
    rejection, a 409 not-ready, or a 5xx). Carries the upstream ``status_code`` and a bounded
    ``detail`` so the engine can map it truthfully rather than reporting it as 'unreachable'."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"registry → {status_code}: {detail}")


def _render_detail(body: str) -> str:
    """Compact a non-2xx upstream body. Prefer a structured error (FastAPI/Pydantic ``detail``) over
    the raw text; fall back to the bounded raw body. Always bounded to 300 chars."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body[:300]
    if isinstance(parsed, dict) and "detail" in parsed:
        detail = parsed["detail"]
        rendered = detail if isinstance(detail, str) else json.dumps(detail, separators=(",", ":"))
        return rendered[:300]
    return json.dumps(parsed, separators=(",", ":"))[:300]


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

    async def execute(self, instance_id: uuid.UUID, input_data: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a configured registry instance synchronously; return its ``ExecutionOut`` JSON.

        The registry contract (proven by #489 PR-1/PR-2): ``POST /api/v1/instances/{id}/execute``
        with body ``{"input_data": {...}}`` → 201 ``ExecutionOut`` (``id``/``status``/
        ``output_data``/…). A transport failure raises ``RegistryClientError``; a reachable non-2xx
        raises ``RegistryRejected`` carrying the status + bounded detail."""
        try:
            resp = await self._client.post(
                f"/api/v1/instances/{instance_id}/execute", json={"input_data": input_data}
            )
        except httpx.HTTPError as exc:  # registry unreachable — clean failure, not a 500
            raise RegistryClientError(f"registry unreachable: {type(exc).__name__}") from exc
        if resp.status_code // 100 != 2:  # reachable but rejected — not unreachable
            raise RegistryRejected(resp.status_code, _render_detail(resp.text))
        out: dict[str, Any] = resp.json()
        return out

    async def list_capability_rows(self) -> list[dict[str, str]]:
        """The CALLER's org's registered tools as ``[{"name": …, "description": …}]`` — the same
        rows ``list_capabilities`` names, plus what each tool DOES.

        #713: the drafter was choosing from a menu of slugs. In compiler run ``a3443e24`` it gave
        ``knowledge-retriever`` — an index over the org's stored documents — to the member whose job
        was reading an unmerged pull request diff, and that member finished ``partial`` in every
        run. The gate cannot catch that (the tool is registered, active and first-party, so ADR-032
        capability-absence has nothing to say) and should not try; fit-for-purpose is a different
        question from existence. Every descriptor row already carries
        ``descriptor.metadata.description``, so the menu could say so all along.

        ``description`` is ``""`` when the row has none — a first-party/seed tool without one is
        normal, and the renderer shows the name alone rather than inventing text. The filter is
        exactly the one ``list_capabilities`` applies (#705: ``kind=tool``, ``active`` only, a
        status-less row treated as active), because the menu and the gate must not drift."""
        try:
            resp = await self._client.get("/api/v1/capabilities", params={"kind": "tool"})
        except httpx.HTTPError as exc:  # registry unreachable — clean failure, not a 500
            raise RegistryClientError(f"registry unreachable: {type(exc).__name__}") from exc
        if resp.status_code // 100 != 2:  # reachable but rejected — not unreachable
            raise RegistryRejected(resp.status_code, _render_detail(resp.text))
        body = resp.json()
        caps = body.get("capabilities") if isinstance(body, dict) else None
        if not isinstance(caps, list):
            return []
        rows: list[dict[str, str]] = []
        for c in caps:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            if c.get("status", "active") != "active":
                continue
            descriptor = c.get("descriptor")
            metadata = descriptor.get("metadata") if isinstance(descriptor, dict) else None
            description = metadata.get("description") if isinstance(metadata, dict) else None
            rows.append(
                {
                    "name": str(c["name"]),
                    "description": str(description) if isinstance(description, str) else "",
                }
            )
        return rows

    async def list_capabilities(self) -> list[str]:
        """The CALLER's org's registered capability NAMES (the registry is org-scoped by the
        downstream headers): ``GET /api/v1/capabilities`` → ``{capabilities:[{name,…}], total}``.

        #638: unioned into the surveyed draft catalog so a deployed connector (e.g. ``GitHub Sink``
        → slug ``github-sink``) is admissible to compile/refine, not only the #596 seed inventory.
        The names are returned RAW — ``survey_catalog``/``_slug`` normalise each to its bare slug.
        Raises like the sibling calls (``RegistryClientError`` unreachable / ``RegistryRejected``
        non-2xx) so the CALLER owns the degrade policy (seed-only on failure — never fail-open).

        #705: this list is the MENU the drafter chooses from, and the compile gate now admits only
        ``active`` TOOL descriptors. So the menu is filtered to match — ``kind=tool`` (a registered
        harness row is not something a member can take as a tool) and ``active`` only (an imported
        MCP tool the org has not approved is refused at dispatch, so offering it can only earn a
        block). Before this, an org with 44 imported tools and 3 approved was shown all 44, and the
        drafter duly picked an unapproved one. A row carrying no ``status`` at all is treated as
        active — an older registry payload should not silently empty the menu.

        #713: derived from ``list_capability_rows`` so the two views cannot drift. This one stays a
        bare-name list because it feeds the VALIDATORS — the capability-absence gate compares slugs,
        and descriptions belong in the prompt, not in what a gate diffs against."""
        return [row["name"] for row in await self.list_capability_rows()]

    async def get_capability(self, capability_id: uuid.UUID) -> dict[str, Any] | None:
        """One registered capability in the CALLER's org, or ``None`` when it does not exist there.

        #695: a compiled team member's ``manifest_ref`` is the registry id of its filed agent
        (ADR-050), and the run resolves each reference through this call before dispatching. A 404 —
        absent, or belonging to another org — is ``None`` so the caller can name the member in a
        clean 422. Any other non-2xx is INCONCLUSIVE and raises, because dispatching a member whose
        agent we could not read would be the fail-open direction."""
        try:
            resp = await self._client.get(f"/api/v1/capabilities/{capability_id}")
        except httpx.HTTPError as exc:  # registry unreachable — clean failure, not a 500
            raise RegistryClientError(f"registry unreachable: {type(exc).__name__}") from exc
        if resp.status_code == httpx.codes.NOT_FOUND:  # absent, or not in the caller's org
            return None
        if resp.status_code // 100 != 2:  # inconclusive — never treated as absent
            raise RegistryClientError(f"registry → {resp.status_code}")
        body: dict[str, Any] = resp.json()
        return body

    async def upsert_harness(
        self, descriptor: dict[str, Any], *, descriptor_id: uuid.UUID
    ) -> uuid.UUID:
        """File a generated agent in the CALLER's org as a ``kind=harness`` capability (#695).

        A compiled team member and a console-built agent are the same object: an ``OHMManifest``
        with ``metadata.kind = "agent"``, produced by the same ``build_subharness``. The console
        builder POSTs its descriptor and keeps the returned id as the agent's ``manifest_ref``; the
        compiler filed nothing, so its agents were unlistable, uneditable, unbindable, and died with
        the run — ``/app/agents`` was empty because nothing had ever been written for it to read.

        FIND-OR-REFRESH, never blind-insert (the #698 precedent: a re-import refreshes an MCP
        server's tools rather than duplicating them). The registry's create inserts keyed on
        ``descriptor_id``, so a blind second POST is a primary-key conflict rather than an update.
        Hence: read the id → present means PUT, absent means POST. An inconclusive read raises
        rather than assuming absence.

        Filed as ``harness``, not ``tool``: ``/app/agents`` reads ``kind=harness`` for the caller's
        org, and the drafter's tool menu is filtered to ``kind=tool`` (#705) — a compiled agent
        filed as a tool would be invisible on the agents page AND offered as a tool to the next
        compile."""
        exists = await self.get_capability(descriptor_id) is not None
        path = f"/api/v1/capabilities/{descriptor_id}" if exists else "/api/v1/capabilities"
        payload: dict[str, Any] = (
            {"descriptor": descriptor}
            if exists
            else {
                "kind": "harness",
                "descriptor": descriptor,
                "descriptor_id": str(descriptor_id),
            }
        )
        try:
            resp = (
                await self._client.put(path, json=payload)
                if exists
                else await self._client.post(path, json=payload)
            )
        except httpx.HTTPError as exc:  # registry unreachable — clean failure, not a 500
            raise RegistryClientError(f"registry unreachable: {type(exc).__name__}") from exc
        if resp.status_code // 100 != 2:  # reachable but rejected — not unreachable
            raise RegistryRejected(resp.status_code, _render_detail(resp.text))
        return descriptor_id

    async def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        """One org-scoped GET → parsed JSON. Transport failure raises ``RegistryClientError``; a
        reachable non-2xx raises ``RegistryRejected`` (the engine maps either to a clean 502 —
        inconclusive is never treated as absent, #664 fail-closed)."""
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:  # registry unreachable — clean failure, not a 500
            raise RegistryClientError(f"registry unreachable: {type(exc).__name__}") from exc
        if resp.status_code // 100 != 2:  # reachable but rejected — not unreachable
            raise RegistryRejected(resp.status_code, _render_detail(resp.text))
        return resp.json()

    async def list_tools(self) -> list[dict[str, Any]]:
        """The CALLER's org's registered tool rows, RAW — ``GET /api/v1/tools`` →
        ``capabilities`` (each with ``id``, ``name``, ``status`` and its ``descriptor``). This is
        the list the harness itself resolves a member's capability ``ref`` against (its
        ``resolve_capability``), so the #664 pre-flight matches a binding by exactly these rows."""
        body = await self._get_json("/api/v1/tools")
        rows = body.get("capabilities") if isinstance(body, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def list_instances(self) -> list[dict[str, Any]]:
        """The CALLER's org's tool instances — ``GET /api/v1/instances`` → ``instances`` (each with
        ``id``, ``capability_id``, ``status``, ``credential_mappings``). Mirrors the harness's own
        ``list_instances``, which is how it finds the org instance a run will bind (#663)."""
        body = await self._get_json("/api/v1/instances")
        rows = body.get("instances") if isinstance(body, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def validate_execution(self, instance_id: uuid.UUID) -> dict[str, Any]:
        """The registry's OWN readiness verdict for one instance —
        ``GET /api/v1/instances/{id}/validate-execution`` → a ``ValidationReport``
        (``is_ready``, ``status``, ``errors[]`` each naming a ``credential_type``).

        #664: the pre-flight asks THIS rather than reading ``status`` off the list row, because
        the verdict also resolves each mapped credential through the broker — a revoked or
        unresolvable credential and a lapsed grant count as missing, not only an unmapped one —
        and it is the same rule that derives ``CONFIGURATION_REQUIRED`` in the first place."""
        body = await self._get_json(f"/api/v1/instances/{instance_id}/validate-execution")
        return body if isinstance(body, dict) else {}

    async def instance_exists(self, instance_id: uuid.UUID) -> bool:
        """True iff a configured instance with this id exists in the CALLER's organisation (the
        registry is org-scoped by the downstream headers). #501-#5: register validates an
        ``adopted_tool_run`` schedule's ``instance_id`` early for a clean 4xx (cross-org already
        fails closed at execute). A 404 — the instance does not exist OR belongs to another org —
        False (the caller rejects fail-fast). Unreachable raises ``RegistryClientError``
        (inconclusive → the caller fails closed rather than admit an unvalidated instance)."""
        try:
            resp = await self._client.get(f"/api/v1/instances/{instance_id}")
        except httpx.HTTPError as exc:  # registry unreachable — inconclusive, fail closed
            raise RegistryClientError(f"registry unreachable: {type(exc).__name__}") from exc
        if resp.status_code == httpx.codes.NOT_FOUND:  # not in the caller's org → reject
            return False
        if resp.status_code // 100 != 2:  # any other non-2xx — inconclusive, fail closed
            raise RegistryClientError(f"registry → {resp.status_code}")
        return True
