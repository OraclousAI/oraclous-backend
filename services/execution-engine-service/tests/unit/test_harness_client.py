"""HarnessClient — request marshalling + error mapping, via a mock transport."""

from __future__ import annotations

import json

import httpx
import pytest
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClient,
    HarnessClientError,
    HarnessRejected,
)

pytestmark = pytest.mark.unit


def _client(handler) -> HarnessClient:  # noqa: ANN001
    return HarnessClient(
        "http://harness", headers={"X-Internal-Key": "k"}, transport=httpx.MockTransport(handler)
    )


async def test_inline_manifest_marshalled_and_status_returned() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["internal"] = request.headers.get("X-Internal-Key")
        return httpx.Response(200, json={"id": "x", "status": "SUCCEEDED", "output": "done"})

    out = await _client(handler).execute(input_text="go", manifest_inline={"ohm_version": "1.0"})
    assert captured["path"] == "/v1/harnesses/execute"
    assert captured["body"] == {"input": "go", "manifest": {"ohm_version": "1.0"}}
    assert captured["internal"] == "k"
    assert out["status"] == "SUCCEEDED"


async def test_manifest_ref_marshalled() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x", "status": "SUCCEEDED"})

    await _client(handler).execute(input_text="go", manifest_ref="cap-123")
    assert captured["body"] == {"input": "go", "manifest_ref": "cap-123"}


async def test_no_manifest_raises() -> None:
    with pytest.raises(HarnessClientError):
        await _client(lambda r: httpx.Response(200)).execute(input_text="go")


async def test_non_2xx_raises_harness_rejected_with_status_and_detail() -> None:
    # A reachable-but-rejecting harness surfaces as HarnessRejected (a HarnessClientError subclass)
    # carrying the upstream status + bounded detail — distinct from a transport failure (#251).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(HarnessRejected) as exc_info:
        await _client(handler).execute(input_text="go", manifest_inline={})
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "bad gateway"
    assert isinstance(exc_info.value, HarnessClientError)  # still a HarnessClientError


async def test_rejected_prefers_structured_ohm_detail() -> None:
    # A 422 with a structured OHM/FastAPI body renders the `detail`, not the raw envelope.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "manifest.ohm_version is required"})

    with pytest.raises(HarnessRejected) as exc_info:
        await _client(handler).execute(input_text="go", manifest_inline={})
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "manifest.ohm_version is required"


async def test_transport_error_becomes_client_error() -> None:
    # harness down / timeout must surface as HarnessClientError (→ a clean FAILED job, never a 500).
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(HarnessClientError):
        await _client(handler).execute(input_text="go", manifest_inline={})


async def test_complete_assignment_marshals_and_wraps_transport() -> None:
    import uuid

    captured: dict = {}

    def ok(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "COMPLETED"})

    aid = uuid.uuid4()
    out = await _client(ok).complete_assignment(aid, "approved")
    assert captured["path"] == f"/v1/harnesses/assignments/{aid}/complete"
    assert captured["body"] == {"output": "approved"}
    assert out["status"] == "COMPLETED"

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(HarnessClientError):  # harness down → clean error, not a 500
        await _client(down).complete_assignment(aid, "x")
