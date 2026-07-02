"""RegistryClient — request marshalling + error mapping, via a mock transport (#489).

The engine-side capability-registry client (a self-contained clone of HarnessClient): it POSTs an
instance /execute and maps a transport failure (RegistryClientError) apart from a reachable
rejection (RegistryRejected, carrying status + bounded detail). Mirrors test_harness_client.py.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from oraclous_execution_engine_service.services.registry_client import (
    RegistryClient,
    RegistryClientError,
    RegistryRejected,
)

pytestmark = pytest.mark.unit

_INSTANCE = uuid.uuid4()


def _client(handler) -> RegistryClient:  # noqa: ANN001
    return RegistryClient(
        "http://registry", headers={"X-Internal-Key": "k"}, transport=httpx.MockTransport(handler)
    )


async def test_execute_marshals_body_and_path_and_returns_json() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["internal"] = request.headers.get("X-Internal-Key")
        return httpx.Response(
            201, json={"id": str(uuid.uuid4()), "status": "SUCCESS", "output_data": {"x": 1}}
        )

    out = await _client(handler).execute(_INSTANCE, {"channel": "email", "content": "hi"})
    assert captured["path"] == f"/api/v1/instances/{_INSTANCE}/execute"
    assert captured["body"] == {"input_data": {"channel": "email", "content": "hi"}}
    assert captured["internal"] == "k"  # the downstream identity header is forwarded
    assert out["status"] == "SUCCESS" and out["output_data"] == {"x": 1}


async def test_execute_accepts_201_created() -> None:
    # the registry execute contract returns 201 — // 100 == 2, so it is NOT treated as a rejection.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": str(uuid.uuid4()), "status": "SUCCESS"})

    out = await _client(handler).execute(_INSTANCE, {})
    assert out["status"] == "SUCCESS"


async def test_non_2xx_raises_registry_rejected_with_status_and_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="instance not ready")

    with pytest.raises(RegistryRejected) as exc_info:
        await _client(handler).execute(_INSTANCE, {})
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "instance not ready"
    assert isinstance(exc_info.value, RegistryClientError)  # still a RegistryClientError


async def test_rejected_prefers_structured_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "input_data.channel is required"})

    with pytest.raises(RegistryRejected) as exc_info:
        await _client(handler).execute(_INSTANCE, {})
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "input_data.channel is required"


async def test_transport_error_becomes_client_error() -> None:
    # registry down / timeout must surface as RegistryClientError (a clean failure, never a 500).
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RegistryClientError):
        await _client(handler).execute(_INSTANCE, {})


# ── instance_exists (#501-#5: register-time org-scoped existence check) ───────────────────────────
async def test_instance_exists_true_on_2xx_and_gets_the_instance() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"], captured["method"] = request.url.path, request.method
        return httpx.Response(200, json={"id": str(_INSTANCE), "status": "READY"})

    assert await _client(handler).instance_exists(_INSTANCE) is True
    assert captured["path"] == f"/api/v1/instances/{_INSTANCE}"  # GET the instance, org-scoped
    assert captured["method"] == "GET"


async def test_instance_exists_false_on_404() -> None:
    # 404 — the instance does not exist OR belongs to another org (registry is org-scoped) → reject.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    assert await _client(handler).instance_exists(_INSTANCE) is False


async def test_instance_exists_raises_on_unreachable() -> None:
    # registry down → inconclusive → RegistryClientError (register fails CLOSED, admits nothing).
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RegistryClientError):
        await _client(handler).instance_exists(_INSTANCE)


async def test_instance_exists_raises_on_5xx() -> None:
    # any other non-2xx is inconclusive too → fail closed rather than admit an unvalidated instance.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="registry unavailable")

    with pytest.raises(RegistryClientError):
        await _client(handler).instance_exists(_INSTANCE)
