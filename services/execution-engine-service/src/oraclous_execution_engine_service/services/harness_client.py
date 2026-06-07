"""Harness-runtime client (ORAA-4 §21 services layer).

The engine composes the harness-runtime over HTTP — it never imports it (four-layer contract: both
are Layer 3, so they talk by API exactly as the harness calls the registry). Identity is propagated
per the trusted-gateway model (ADR-018): the caller passes the already-built downstream headers
(gateway headers + the internal key; dev: a bearer), so the harness sees the same tenant.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx


class HarnessClientError(Exception):
    """A harness-runtime call failed (non-2xx or transport error)."""


class HarnessTimeout(HarnessClientError):
    """The harness call exceeded its (per-job) wall-clock budget → the engine marks it TIMED_OUT."""


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

    async def complete_assignment(self, assignment_id: uuid.UUID, output: str) -> dict[str, Any]:
        """Complete a human task-board assignment — the harness marks it COMPLETED and flips the
        parked run ESCALATED → SUCCEEDED with ``output``. Used by the engine task board (S4)."""
        try:
            resp = await self._client.post(
                f"/v1/harnesses/assignments/{assignment_id}/complete", json={"output": output}
            )
        except httpx.HTTPError as exc:  # harness unreachable — clean failure, not a 500
            raise HarnessClientError(f"harness unreachable: {type(exc).__name__}") from exc
        if resp.status_code // 100 != 2:
            raise HarnessClientError(f"complete assignment → {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        manifest_ref: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — forwarded to httpx, not an asyncio cancel
    ) -> dict[str, Any]:
        """Run a harness to completion/escalation and return its ``HarnessExecutionOut`` JSON.

        Supply exactly one of ``manifest_inline`` (a parsed OHM object) or ``manifest_ref`` (a
        registered harness id). ``timeout`` (the job's declared wall-clock) overrides the client
        default for this call; exceeding it raises ``HarnessTimeout`` → the job is timed out."""
        body: dict[str, Any] = {"input": input_text}
        if manifest_inline is not None:
            body["manifest"] = manifest_inline
        elif manifest_ref is not None:
            body["manifest_ref"] = manifest_ref
        else:
            raise HarnessClientError("a manifest_inline or manifest_ref is required")
        kwargs: dict[str, Any] = {"json": body}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = await self._client.post("/v1/harnesses/execute", **kwargs)
        except httpx.ReadTimeout as exc:  # the run exceeded the declared wall-clock → TIMED_OUT
            raise HarnessTimeout(f"harness call timed out: {type(exc).__name__}") from exc
        except (
            httpx.HTTPError
        ) as exc:  # transport (incl. connect/pool timeouts) → unreachable, FAILED
            raise HarnessClientError(f"harness unreachable: {type(exc).__name__}") from exc
        if resp.status_code // 100 != 2:
            raise HarnessClientError(f"harness execute → {resp.status_code}: {resp.text[:300]}")
        return resp.json()
