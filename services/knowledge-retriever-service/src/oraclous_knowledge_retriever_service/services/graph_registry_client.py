"""Accessible-graph-set enumeration (ORAA-4 §21 services layer) — the ADR-026 federation seam.

A federated query may fan out over EXACTLY the graphs the caller can already read individually —
the org-scoped set in the KGS Postgres `knowledge_graphs` registry. KRS cannot reach that registry
itself (no Postgres access here, and the import-linter same-tier contract forbids importing the
sibling service), so the enumeration is the knowledge-graph-service's internal endpoint
``GET /internal/v1/graphs`` over the ADR-018 internal trust plane: the caller's verified identity
is forwarded as ``X-Principal-*``/``X-Organisation-Id`` gated by the shared ``X-Internal-Key``.
``dev`` mode forwards the fixed dev bearer instead, so the loop runs key-free in dev/CI; ``gateway``
AND ``jwt`` modes BOTH forward the verified principal + org headers (the KGS internal plane trusts
the X-Principal-*/X-Internal-Key shape regardless of how KRS itself authenticated the caller — a
jwt-mode KRS still reaches the internal plane the same way, so an all-jwt deployment does NOT 503).

The HTTP client is a POOLED ``httpx.AsyncClient`` (built once at lifespan with the registry base url
+ configured timeout), reused across requests — not a fresh client per fan-out. Fail-closed: an
unreachable registry, a non-200, or a malformed body raises :class:`GraphRegistryError` (→ 503 at
the route) — federation NEVER guesses the accessible set.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class GraphRegistryError(Exception):
    """The graph registry could not be enumerated (unreachable / bad response). Maps to 503."""


@dataclass(frozen=True)
class GraphInfo:
    """One accessible graph: its id (str UUID) + display name (for result labeling)."""

    id: str
    name: str


class GraphRegistryClient:
    """Enumerates the caller's accessible graphs from the KGS internal plane (ADR-018), over a
    pooled httpx client."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        auth_mode: str,
        dev_bearer: str,
        internal_service_key: str | None,
    ) -> None:
        self._client = client
        self._auth_mode = auth_mode
        self._dev_bearer = dev_bearer
        self._internal_service_key = internal_service_key

    def _headers(self, principal) -> dict[str, str]:
        """Identity to forward to KGS — the same downstream-identity shape the capability
        registry's first-party connectors use (ADR-018). ``dev`` → the fixed bearer. ``gateway``
        AND ``jwt`` → the caller's verified principal + org headers gated by the shared internal
        key (the internal plane trusts that shape independent of KRS's own auth mode, so jwt mode
        forwards it too rather than structurally 503-ing)."""
        if self._auth_mode == "dev":
            return {"Authorization": f"Bearer {self._dev_bearer}"}
        headers = {
            "X-Principal-Id": str(principal.principal_id),
            "X-Principal-Type": principal.principal_type.value,
            "X-Organisation-Id": str(principal.organisation_id),
        }
        if self._internal_service_key:
            headers["X-Internal-Key"] = self._internal_service_key
        return headers

    async def accessible_graphs(self, principal) -> list[GraphInfo]:
        """The graphs the caller can read (org-scoped), in registry order (newest first)."""
        try:
            resp = await self._client.get("/internal/v1/graphs", headers=self._headers(principal))
        except httpx.HTTPError as exc:
            raise GraphRegistryError("the graph registry could not be reached") from exc
        if resp.status_code != 200:
            raise GraphRegistryError(f"the graph registry returned {resp.status_code}")
        try:
            body = resp.json()
            graphs = body["graphs"]
            return [GraphInfo(id=str(g["id"]), name=str(g["name"])) for g in graphs]
        except (ValueError, KeyError, TypeError) as exc:
            raise GraphRegistryError("the graph registry returned a malformed body") from exc
