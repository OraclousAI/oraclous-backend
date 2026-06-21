"""core/evaluate client (services layer) — the engine grades a completed team run.

The engine composes the knowledge-retriever's ``core/evaluate`` (the flow judge) over HTTP — it
never imports it (both are Layer-3-and-below; they talk by API). Identity is propagated per the
trusted-gateway model (ADR-018): the caller passes the already-built downstream headers (gateway
headers + the internal key; dev: a bearer), so the judge server-stamps the verdict's org from THIS
principal (ADR-037 H2 — the engine never puts ``organisation_id`` in the body, and the request
schema has no such field). Returns the raw Verdict JSON (a dict) so the engine stays decoupled from
packages/eval — it stores the dict and reads ``score``/``pass``/``recommended_action`` from it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class EvaluateClientError(Exception):
    """A core/evaluate call failed — the BASE/transport case: the judge was UNREACHABLE (a
    connect/pool error). A reachable-but-rejecting judge raises ``EvaluateRejected`` instead, so the
    gate can tell a transport failure apart from a judge rejection (mirrors HarnessRejected)."""


class EvaluateRejected(EvaluateClientError):
    """core/evaluate WAS reachable and answered non-2xx — e.g. 422 (judge-not-configured, or a
    ``battery:`` token reaching KRS, which it refuses), 429 (EvaluationCapacityExceeded), or 5xx.
    Carries the upstream ``status_code`` + a bounded ``detail`` so the gate records a truthful
    fail-closed verdict rather than reporting 'unreachable'."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"core/evaluate → {status_code}: {detail}")


def _render_detail(body: str) -> str:
    """Compact a non-2xx upstream body (prefer a structured ``detail``), bounded to 300 chars."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body[:300]
    if isinstance(parsed, dict) and "detail" in parsed:
        detail = parsed["detail"]
        rendered = detail if isinstance(detail, str) else json.dumps(detail, separators=(",", ":"))
        return rendered[:300]
    return json.dumps(parsed, separators=(",", ":"))[:300]


class EvaluateClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str],
        timeout: float = 35.0,
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

    async def evaluate(
        self,
        *,
        target_ref: str,
        target_output: str,
        success_criteria: str,
        target_kind: str = "run",
        pass_threshold: float = 0.7,
        judge_credential_id: str | None = None,
        judge_model: str | None = None,
    ) -> dict[str, Any]:
        """Grade ``target_output`` against ``success_criteria`` (prose — NEVER a ``battery:`` token;
        the battery is iterated engine-side and only each check's prose rubric is sent here).

        Returns the Verdict JSON. Raises ``EvaluateRejected`` on a reachable non-2xx and
        ``EvaluateClientError`` on transport/timeout — the gate maps both to a fail-closed verdict
        (the run still SUCCEEDS)."""
        body: dict[str, Any] = {
            "target_kind": target_kind,
            "target_ref": target_ref,
            "target_output": target_output,
            "success_criteria": success_criteria,  # NO organisation_id — server-stamped (H2)
            "pass_threshold": pass_threshold,
        }
        # BYOM judge (ADR-037 / BYOM-judge): when the manifest declares an evaluator credential, KRS
        # resolves THAT per-org key from the broker and grades with the user's own key. Only sent
        # when present, so the operator-key path is unchanged. KRS owns the model-binding split.
        if judge_credential_id is not None:
            body["judge_credential_id"] = judge_credential_id
        if judge_model is not None:
            body["judge_model"] = judge_model
        try:
            resp = await self._client.post("/internal/v1/evaluate", json=body)
        except httpx.HTTPError as exc:  # transport (connect/pool/read timeout) → unreachable
            raise EvaluateClientError(f"core/evaluate unreachable: {type(exc).__name__}") from exc
        if resp.status_code // 100 != 2:  # reachable but rejected — not unreachable
            raise EvaluateRejected(resp.status_code, _render_detail(resp.text))
        result: dict[str, Any] = resp.json()
        return result
