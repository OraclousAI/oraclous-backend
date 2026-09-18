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


#: The producer (#728 provenance) fields the KGS internal ingest route actually reads, and the same
#: six ``GraphIngestConnector._producer_config`` forwards on the tool path. A caller's stamp is
#: FILTERED through this tuple rather than forwarded verbatim, so a stray key (e.g. the engine's own
#: ``attempt_id``, which the route does not know) never rides along unnoticed.
_PRODUCER_WIRE_FIELDS = (
    "producer_kind",
    "team_run_id",
    "member_role",
    "execution_id",
    "team_id",
    "ordinal",
)


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

    async def list_artifacts(
        self,
        graph_id: uuid.UUID | str,
        *,
        team_run_id: uuid.UUID | str | None = None,
        member_role: str | None = None,
    ) -> list[dict[str, Any]]:
        """The artifacts landed on ``graph_id`` in the CALLER's organisation (KGS is org-scoped by
        the downstream headers). A 404 — the graph does not exist OR belongs to another org — is an
        empty list (nothing landed for this caller). Any other non-2xx raises (inconclusive → the
        done-check fails closed). Never logs artifact contents.

        #1137: ``team_run_id``/``member_role`` narrow the listing to one run's one member — the
        single read the platform's settle-time autosave duplicate guard makes. Both are OPTIONAL and
        are omitted from the query entirely when not given, so the #552 done-check's request is
        byte-for-byte what it always was (never a literal ``team_run_id=None``)."""
        params = {"graph_id": str(graph_id)}
        if team_run_id is not None:
            params["team_run_id"] = str(team_run_id)
        if member_role is not None:
            params["member_role"] = member_role
        try:
            resp = await self._client.get("/v1/artifacts", params=params)
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

    async def ingest(
        self,
        graph_id: uuid.UUID | str,
        *,
        content: str,
        source_type: str = "text",
        producer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one document to the KGS's internal ingest route and return the enqueued job.

        #1137 — the platform's OWN settle-time save. It POSTs the same ``/internal/v1/ingest`` the
        capability-registry ``graph-ingest`` connector already uses for the tool path (Layer-3 to
        Layer-1 by API, identity riding the ADR-018 downstream headers the CALLER built from its
        verified principal — this client never re-derives or re-checks identity, and the org is
        never a body field, ORG001).

        ``producer`` is FILTERED through ``_PRODUCER_WIRE_FIELDS`` (``None`` values dropped, never
        sent as a literal null) rather than forwarded verbatim, so only the fields the route reads
        cross the wire. No ``title`` is ever set: the KGS's ``derive_name`` falls back to the
        producing member's role.

        Unlike ``list_artifacts``' 404-is-empty READING, any non-2xx here is a genuine write failure
        and always raises ``ArtifactsClientError``. The upstream response body (which can carry
        another tenant's identifiers) is never echoed into the message."""
        body: dict[str, Any] = {
            "graph_id": str(graph_id),
            "content": content,
            "source_type": source_type,
        }
        for key in _PRODUCER_WIRE_FIELDS:
            value = (producer or {}).get(key)
            if value is not None:
                body[key] = value
        try:
            resp = await self._client.post("/internal/v1/ingest", json=body)
        except httpx.HTTPError as exc:  # KGS unreachable — the write did not happen
            raise ArtifactsClientError(
                f"knowledge-graph-service unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code // 100 != 2:
            raise ArtifactsClientError(f"knowledge-graph-service → {resp.status_code}")
        payload = resp.json()
        return dict(payload) if isinstance(payload, dict) else {}
