"""Knowledge-graph client (services layer) — the engine's org-scoped graph existence check.

A graph-bound team run (#524, ADR-040 Decision 7) names a ``graph_id`` once; the engine validates it
fail-fast at create by GETting the graph from the knowledge-graph-service. The engine never imports
KGS (KGS is Layer-1 substrate, the engine Layer-3 — they talk by API). Identity is propagated per
the trusted-gateway model (ADR-018): the caller passes the already-built downstream headers, so KGS
scopes the read to the SAME tenant — a graph the caller's org does not own returns 404 (→ rejected).
"""

from __future__ import annotations

import uuid

import httpx


class GraphClientError(Exception):
    """The KGS graph GET could not be completed (unreachable, or a non-2xx/404 response). The engine
    maps it to a 502 at create — validation is inconclusive, so fail closed rather than admit an
    unvalidated graph."""


class GraphClient:
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

    async def graph_exists(self, graph_id: uuid.UUID | str) -> bool:
        """True iff a graph with this id exists in the CALLER's organisation (KGS is org-scoped by
        the downstream headers). A 404 — the graph does not exist OR belongs to another org —
        returns False (the caller rejects fail-fast). Other failures raise ``GraphClientError``."""
        try:
            resp = await self._client.get(f"/api/v1/graphs/{graph_id}")
        except httpx.HTTPError as exc:  # KGS unreachable — inconclusive, fail closed
            raise GraphClientError(
                f"knowledge-graph-service unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code == httpx.codes.NOT_FOUND:  # not in the caller's org → reject
            return False
        if resp.status_code // 100 != 2:  # any other non-2xx — inconclusive, fail closed
            raise GraphClientError(f"knowledge-graph-service → {resp.status_code}")
        return True
