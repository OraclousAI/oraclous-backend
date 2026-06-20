"""Harness-runtime client (ORAA-4 §21 services layer).

The engine composes the harness-runtime over HTTP — it never imports it (four-layer contract: both
are Layer 3, so they talk by API exactly as the harness calls the registry). Identity is propagated
per the trusted-gateway model (ADR-018): the caller passes the already-built downstream headers
(gateway headers + the internal key; dev: a bearer), so the harness sees the same tenant.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx


class HarnessClientError(Exception):
    """A harness-runtime call failed — the BASE/transport case: the harness was unreachable (a
    connect/pool error). A reachable-but-rejecting harness raises ``HarnessRejected`` instead, so
    the engine can tell a transport failure apart from an OHM rejection (ORAA #251)."""


class HarnessRejected(HarnessClientError):
    """The harness WAS reachable and answered with a non-2xx response (e.g. a 422 OHM-validation
    rejection, or a 5xx). Carries the upstream ``status_code`` and a bounded ``detail`` so the
    engine can map it into a truthful taxonomy rather than reporting it as 'unreachable'."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"harness → {status_code}: {detail}")


class HarnessTimeout(HarnessClientError):
    """The harness call exceeded its (per-job) wall-clock budget → the engine marks it TIMED_OUT."""


def _render_detail(body: str) -> str:
    """Compact a non-2xx upstream body. Prefer a structured OHM error (FastAPI/Pydantic ``detail``)
    over the raw text; fall back to the bounded raw body. Always bounded to 300 chars."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body[:300]
    if isinstance(parsed, dict) and "detail" in parsed:
        detail = parsed["detail"]
        rendered = detail if isinstance(detail, str) else json.dumps(detail, separators=(",", ":"))
        return rendered[:300]
    return json.dumps(parsed, separators=(",", ":"))[:300]


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
        if resp.status_code // 100 != 2:  # reachable but rejected — not unreachable (#251)
            raise HarnessRejected(resp.status_code, _render_detail(resp.text))
        return resp.json()

    async def resume(
        self, execution_id: uuid.UUID, decision: str, decision_reason: str | None = None
    ) -> dict[str, Any]:
        """Resolve a mid-loop HITL pause — APPROVED resumes the loop (the gated tool runs), DENIED
        terminates the run FAILED. Returns the updated ``HarnessExecutionOut``. Used by the engine
        task board (S6)."""
        body: dict[str, Any] = {"decision": decision}
        if decision_reason is not None:
            body["decision_reason"] = decision_reason
        try:
            resp = await self._client.post(f"/v1/harnesses/{execution_id}/resume", json=body)
        except httpx.HTTPError as exc:  # harness unreachable — clean failure, not a 500
            raise HarnessClientError(f"harness unreachable: {type(exc).__name__}") from exc
        if resp.status_code // 100 != 2:  # reachable but rejected — not unreachable (#251)
            raise HarnessRejected(resp.status_code, _render_detail(resp.text))
        return resp.json()

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        manifest_ref: str | None = None,
        capability_ceiling: list[str] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — forwarded to httpx, not an asyncio cancel
    ) -> dict[str, Any]:
        """Run a harness to completion/escalation and return its ``HarnessExecutionOut`` JSON.

        Supply exactly one of ``manifest_inline`` (a parsed OHM object) or ``manifest_ref`` (a
        registered harness id). ``capability_ceiling`` (a team member's ``tools[]``) caps the
        harness's runtime ceiling, fail-closed (ADR-032/035 §5). ``timeout`` (the job's declared
        wall-clock) overrides the client default; exceeding it raises ``HarnessTimeout``."""
        body: dict[str, Any] = {"input": input_text}
        if manifest_inline is not None:
            body["manifest"] = manifest_inline
        elif manifest_ref is not None:
            body["manifest_ref"] = manifest_ref
        else:
            raise HarnessClientError("a manifest_inline or manifest_ref is required")
        if capability_ceiling is not None:
            body["capability_ceiling"] = capability_ceiling
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
        if resp.status_code // 100 != 2:  # reachable but rejected — not unreachable (#251)
            raise HarnessRejected(resp.status_code, _render_detail(resp.text))
        return resp.json()
