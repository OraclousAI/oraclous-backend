"""Artifacts client (services layer) — the engine's org-scoped read of a graph's LANDED artifacts.

The coded loop done-check (ADR-043 #552) confirms a loop converged only when its members' outputs
actually LANDED on the team's shared graph — not merely that the coordinator believed it was done.
The engine never imports the knowledge-graph-service (KGS is Layer-1 substrate, the engine Layer-3 —
they talk by API); identity rides the trusted-gateway model (ADR-018): the caller passes the already
-built downstream headers, so KGS scopes the read to the SAME tenant (a graph the org does not own
returns 404 → an empty list → not-yet-landed, fail-closed). Mirrors ``GraphClient``.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx


class ArtifactsClientError(Exception):
    """The KGS artifacts GET could not be completed (unreachable, or a non-2xx/non-404 response).
    The done-check treats it as not-yet-converged (fail-closed) rather than asserting convergence on
    an inconclusive read."""


class ArtifactsClient:
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

    async def list_artifacts(self, graph_id: uuid.UUID | str) -> list[dict[str, Any]]:
        """The artifacts landed on ``graph_id`` in the CALLER's organisation (KGS is org-scoped by
        the downstream headers). A 404 — the graph does not exist OR belongs to another org — is an
        empty list (nothing landed for this caller). Any other non-2xx raises (inconclusive → the
        done-check fails closed). Never logs artifact contents."""
        try:
            resp = await self._client.get("/v1/artifacts", params={"graph_id": str(graph_id)})
        except httpx.HTTPError as exc:  # KGS unreachable — inconclusive
            raise ArtifactsClientError(
                f"knowledge-graph-service unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code == httpx.codes.NOT_FOUND:
            return []
        if resp.status_code // 100 != 2:
            raise ArtifactsClientError(f"knowledge-graph-service → {resp.status_code}")
        body = resp.json()
        return list(body) if isinstance(body, list) else []
