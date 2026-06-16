"""KGS graph-membership client (ORAA-4 §21 services layer; ADR-029 §3).

The registry has no graph table and knowledge-graph-service exposes no graph
``owner_organization_id``, so the binding's graph side is verified by a KGS *membership* call:
``GET /internal/v1/graphs`` over the ADR-018 internal trust plane. The caller's verified identity is
FORWARDED (``X-Principal-*``/``X-Organisation-Id`` gated by the shared ``X-Internal-Key`` — the same
forward-and-trust idiom the ``graph_ingest`` connector uses), so KGS scopes the listing to the
caller's org and never leaks another tenant's graphs. ``dev`` mode forwards the fixed dev bearer
instead (the KGS resolves it to the shared dev org), so the binding flow runs key-free in dev/CI.

The returned set (id → name) is used two ways (ADR-029): on attach, to confirm the target
``graph_id`` is visible to the caller (absent → 404); on the read paths, to filter out + name the
surviving ``graph_id``s (a dangling row whose graph was deleted in KGS is silently skipped, §4).
Fail-closed: an unreachable KGS, a non-200, or a malformed body raises :class:`GraphMembershipError`
(→ 503 at the route) — the binding flow NEVER guesses the accessible set.
"""

from __future__ import annotations

import uuid

import httpx

from oraclous_capability_registry_service.core.config import get_settings

_TIMEOUT_S = 10.0
_GRAPHS_PATH = "/internal/v1/graphs"


class GraphMembershipError(Exception):
    """The KGS graph membership could not be enumerated (unreachable / bad body). Maps to 503."""


class GraphMembershipClient:
    """Enumerates the caller's accessible graphs from the KGS internal plane (ADR-018)."""

    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    def _headers(self, *, organisation_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, str]:
        """Identity to forward to KGS (ADR-018) — the same downstream-identity shape the first-party
        connectors use. ``dev`` → the fixed bearer (resolved to the shared dev org by KGS).
        ``gateway``/``jwt`` → the caller's verified principal + org headers gated by the shared
        internal key, so KGS scopes the listing to the SAME tenant (a binding can never reference
        another org's graph)."""
        settings = get_settings()
        if settings.AUTH_MODE == "dev":
            return {"Authorization": f"Bearer {settings.DEV_BEARER}"}
        headers = {
            "X-Principal-Id": str(user_id),
            "X-Principal-Type": "user",
            "X-Organisation-Id": str(organisation_id),
        }
        if settings.INTERNAL_SERVICE_KEY:
            headers["X-Internal-Key"] = settings.INTERNAL_SERVICE_KEY
        return headers

    async def accessible_graphs(
        self, *, organisation_id: uuid.UUID, user_id: uuid.UUID
    ) -> dict[uuid.UUID, str]:
        """The graphs the caller's org can read, as a ``{graph_id: name}`` map (ADR-026).

        Fail-closed: any transport error / non-200 / malformed body raises ``GraphMembershipError``.
        """
        settings = get_settings()
        try:
            async with httpx.AsyncClient(
                base_url=settings.KNOWLEDGE_GRAPH_URL.rstrip("/"),
                timeout=_TIMEOUT_S,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                resp = await client.get(
                    _GRAPHS_PATH,
                    headers=self._headers(organisation_id=organisation_id, user_id=user_id),
                )
        except httpx.HTTPError as exc:
            raise GraphMembershipError("the knowledge graph service could not be reached") from exc
        if resp.status_code != 200:
            raise GraphMembershipError(f"the knowledge graph service returned {resp.status_code}")
        try:
            body = resp.json()
            graphs = body["graphs"]
            return {uuid.UUID(str(g["id"])): str(g["name"]) for g in graphs}
        except (ValueError, KeyError, TypeError) as exc:
            raise GraphMembershipError(
                "the knowledge graph service returned a malformed body"
            ) from exc
