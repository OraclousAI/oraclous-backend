"""Credential-broker client (ORAA-4 §21 services layer) — BYOM judge key resolution.

core/evaluate resolves a per-org BYOM **judge** credential (an OpenRouter API key) the same way the
harness resolves a BYOM model credential: the broker's internal, org-scoped
``/internal/resolve-credential`` gated by ``X-Internal-Key`` (ADR-008 — the broker decrypts it; it
is held in memory only for the request). KRS never stores model/judge keys itself. Mirrors the
harness-runtime ``BrokerClient`` byte-for-byte.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx


class BrokerError(Exception):
    """The credential-broker could not resolve a credential."""


class BrokerClient:
    def __init__(
        self,
        base_url: str,
        *,
        internal_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Internal-Key": internal_key, "Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def resolve_credential(
        self, *, credential_id: str, organisation_id: uuid.UUID
    ) -> dict[str, Any]:
        """Return the decrypted credential payload (e.g. ``{"api_key": ...}``) — org-scoped."""
        resp = await self._client.post(
            "/internal/resolve-credential",
            json={"organisation_id": str(organisation_id), "credential_id": credential_id},
        )
        if resp.status_code == 404:
            raise BrokerError(f"credential {credential_id} not found")
        if resp.status_code // 100 != 2:
            raise BrokerError(f"resolve-credential → {resp.status_code}: {resp.text[:200]}")
        payload: dict[str, Any] = resp.json().get("credential") or {}
        return payload
