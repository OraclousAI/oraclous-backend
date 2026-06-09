"""Cred-broker webhook-secret client (ORAA-4 §21 services layer).

Mints + resolves a webhook signing secret over the broker's X-Internal-Key ``/internal`` endpoints
(the ADR-008 home for recoverable secret material). The gateway holds only the broker secret id;
the plaintext is fetched transiently at verify time and never stored or logged here.
"""

from __future__ import annotations

import json
import uuid

from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient


class BrokerSecretError(Exception):
    """The broker could not mint/resolve a webhook secret (a non-2xx that is not a 404)."""


class WebhookSecretClient:
    def __init__(
        self, *, upstream_client: UpstreamClient, broker_base_url: str, internal_key: str
    ) -> None:
        self._upstream = upstream_client
        self._base_url = broker_base_url.rstrip("/")
        self._internal_key = internal_key

    def _headers(self) -> list[tuple[bytes, bytes]]:
        return [
            (b"content-type", b"application/json"),
            (b"x-internal-key", self._internal_key.encode("latin-1")),
        ]

    async def mint(self, *, organisation_id: uuid.UUID, secret: str) -> uuid.UUID:
        body = json.dumps({"organisation_id": str(organisation_id), "secret": secret}).encode()
        resp = await self._upstream.open(
            method="POST",
            url=f"{self._base_url}/internal/webhook-secrets",
            headers=self._headers(),
            params=None,
            content=body,
        )
        try:
            code, raw = resp.status_code, await resp.aread()
        finally:
            await resp.aclose()
        if code not in (200, 201):
            raise BrokerSecretError(f"broker mint returned {code}")
        try:
            return uuid.UUID(str(json.loads(raw)["secret_id"]))
        except (ValueError, TypeError, KeyError) as exc:
            raise BrokerSecretError("broker mint returned a malformed body") from exc

    async def resolve(self, *, organisation_id: uuid.UUID, secret_id: uuid.UUID) -> str | None:
        """The plaintext signing secret, or None if the broker can't resolve it (404 — treat as a
        fail-closed reject upstream, never a pass-through)."""
        body = json.dumps(
            {"organisation_id": str(organisation_id), "secret_id": str(secret_id)}
        ).encode()
        resp = await self._upstream.open(
            method="POST",
            url=f"{self._base_url}/internal/webhook-secrets/resolve",
            headers=self._headers(),
            params=None,
            content=body,
        )
        try:
            code, raw = resp.status_code, await resp.aread()
        finally:
            await resp.aclose()
        if code == 404:
            return None
        if code not in (200, 201):
            raise BrokerSecretError(f"broker resolve returned {code}")
        try:
            return str(json.loads(raw)["secret"])
        except (ValueError, TypeError, KeyError) as exc:
            raise BrokerSecretError("broker resolve returned a malformed body") from exc
