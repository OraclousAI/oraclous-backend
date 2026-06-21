"""EvaluateClient — core/evaluate request marshalling + error mapping via a mock transport (#477).

Mirrors test_harness_client. Proves the engine posts the right body (and never an org id, H2),
returns the Verdict JSON, and maps a reachable non-2xx to EvaluateRejected vs a transport error to
EvaluateClientError (so the gate can fail-closed without reporting a rejection as 'unreachable')."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from oraclous_execution_engine_service.services.evaluate_client import (
    EvaluateClient,
    EvaluateClientError,
    EvaluateRejected,
)

pytestmark = pytest.mark.unit


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> EvaluateClient:
    return EvaluateClient(
        "http://krs", headers={"X-Internal-Key": "k"}, transport=httpx.MockTransport(handler)
    )


async def test_marshals_request_and_returns_verdict() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.url.path == "/internal/v1/evaluate"
        return httpx.Response(
            200, json={"pass": True, "score": 0.8, "recommended_action": "accept"}
        )

    out = await _client(handler).evaluate(
        target_ref="run-1", target_output="the answer", success_criteria="is correct"
    )
    assert out["score"] == 0.8 and out["pass"] is True
    assert seen["target_kind"] == "run" and seen["success_criteria"] == "is correct"
    assert "organisation_id" not in seen  # H2 — the org is server-stamped, never in the body


async def test_non_2xx_raises_evaluate_rejected_with_status_and_detail() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "eval_judge_not_configured"})

    with pytest.raises(EvaluateRejected) as exc:
        await _client(handler).evaluate(target_ref="r", target_output="o", success_criteria="c")
    assert exc.value.status_code == 422 and "judge" in exc.value.detail


async def test_transport_error_raises_client_error_not_rejected() -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(EvaluateClientError) as exc:
        await _client(boom).evaluate(target_ref="r", target_output="o", success_criteria="c")
    assert not isinstance(exc.value, EvaluateRejected)  # unreachable, not a rejection
