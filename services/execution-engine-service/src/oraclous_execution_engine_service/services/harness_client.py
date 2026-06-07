"""Harness-runtime client (ORAA-4 §21 services layer).

The engine composes the harness-runtime over HTTP — it never imports it (four-layer contract: both
are Layer 3, so they talk by API exactly as the harness calls the registry). Identity is propagated
per the trusted-gateway model (ADR-018): the caller passes the already-built downstream headers
(gateway headers + the internal key; dev: a bearer), so the harness sees the same tenant.
"""

from __future__ import annotations

from typing import Any

import httpx


class HarnessClientError(Exception):
    """A harness-runtime call failed (non-2xx or transport error)."""


class HarnessClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str],
        timeout: float = 600.0,
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

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        manifest_ref: str | None = None,
    ) -> dict[str, Any]:
        """Run a harness to completion/escalation and return its ``HarnessExecutionOut`` JSON.

        Supply exactly one of ``manifest_inline`` (a parsed OHM object) or ``manifest_ref`` (a
        registered harness id) — mirroring the harness ``/execute`` contract."""
        body: dict[str, Any] = {"input": input_text}
        if manifest_inline is not None:
            body["manifest"] = manifest_inline
        elif manifest_ref is not None:
            body["manifest_ref"] = manifest_ref
        else:
            raise HarnessClientError("a manifest_inline or manifest_ref is required")
        resp = await self._client.post("/v1/harnesses/execute", json=body)
        if resp.status_code // 100 != 2:
            raise HarnessClientError(f"harness execute → {resp.status_code}: {resp.text[:300]}")
        return resp.json()
