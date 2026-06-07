"""HarnessClient — request marshalling + error mapping, via a mock transport."""

from __future__ import annotations

import json

import httpx
import pytest
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClient,
    HarnessClientError,
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


async def test_non_2xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(HarnessClientError):
        await _client(handler).execute(input_text="go", manifest_inline={})
