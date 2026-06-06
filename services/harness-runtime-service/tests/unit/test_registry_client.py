"""Registry client (slice 3 hardening): ref↔capability_id binding closes the allocation bypass."""

from __future__ import annotations

import httpx
import pytest
from oraclous_harness_runtime_service.services.registry_client import RegistryClient, RegistryError

pytestmark = pytest.mark.unit

# Two registry tools: a benign echo and a (governance-forbidden) shell-exec.
_TOOLS = {
    "capabilities": [
        {"id": "11111111-1111-1111-1111-111111111111", "name": "Echo", "descriptor": {}},
        {"id": "22222222-2222-2222-2222-222222222222", "name": "Shell Exec", "descriptor": {}},
    ]
}


def _client() -> RegistryClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/tools":
            return httpx.Response(200, json=_TOOLS)
        return httpx.Response(404, json={"detail": "not found"})

    return RegistryClient("http://registry", headers={}, transport=httpx.MockTransport(handler))


async def test_resolve_by_ref_name() -> None:
    item = await (_client()).resolve_capability("core/echo@1.0.0")
    assert item["id"] == "11111111-1111-1111-1111-111111111111"


async def test_capability_id_must_match_the_ref_name() -> None:
    # benign ref "echo" but capability_id points at shell-exec → rejected (no allocation bypass).
    with pytest.raises(RegistryError):
        await (_client()).resolve_capability(
            "core/echo@1.0.0", explicit_id="22222222-2222-2222-2222-222222222222"
        )


async def test_capability_id_matching_ref_is_accepted() -> None:
    item = await (_client()).resolve_capability(
        "core/echo@1.0.0", explicit_id="11111111-1111-1111-1111-111111111111"
    )
    assert item["name"] == "Echo"
