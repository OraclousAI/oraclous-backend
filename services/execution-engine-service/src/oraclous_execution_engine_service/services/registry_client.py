"""Capability-registry client (ORAA-4 §21 services layer).

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
