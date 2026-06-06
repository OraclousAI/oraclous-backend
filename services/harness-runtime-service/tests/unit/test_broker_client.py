"""Credential-broker client (slice 4): org-scoped BYOM resolution via the internal resolve route."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from oraclous_harness_runtime_service.services.broker_client import BrokerClient, BrokerError

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()


def _client(handler) -> BrokerClient:  # noqa: ANN001
    return BrokerClient(
        "http://broker", internal_key="dev-internal-key", transport=httpx.MockTransport(handler)
    )


async def test_resolves_api_key_payload() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["key"] = request.headers.get("X-Internal-Key")
        return httpx.Response(200, json={"credential": {"api_key": "sk-or-xxx"}})

    payload = await _client(handler).resolve_credential(credential_id="c1", organisation_id=_ORG)
    assert payload == {"api_key": "sk-or-xxx"}
    assert captured["key"] == "dev-internal-key"
    assert captured["body"] == {"organisation_id": str(_ORG), "credential_id": "c1"}


async def test_missing_credential_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    with pytest.raises(BrokerError):
        await _client(handler).resolve_credential(credential_id="nope", organisation_id=_ORG)
