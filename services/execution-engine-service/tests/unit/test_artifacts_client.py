"""ArtifactsClient (ADR-043 #552) — the engine's org-scoped read of a graph's LANDED artifacts,
used by the coded loop done-check to confirm a loop's work actually persisted. Mirrors GraphClient:
GET /v1/artifacts?graph_id=<id> with the downstream headers (KGS scopes by org); a 404 is an empty
list (nothing landed for this caller); any other non-2xx is inconclusive → a fail-closed raise.

RED until ``artifacts_client`` lands — imported function-locally so the module still collects.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = pytest.mark.unit


def _client(handler: Callable[[httpx.Request], httpx.Response]):
    from oraclous_execution_engine_service.services.artifacts_client import ArtifactsClient

    return ArtifactsClient(
        "http://kgs", headers={"X-Internal-Key": "k"}, transport=httpx.MockTransport(handler)
    )


async def test_lists_artifacts_org_scoped_by_graph_id_query() -> None:
    gid = uuid.uuid4()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["graph_id"] = request.url.params.get("graph_id")
        seen["key"] = request.headers.get("X-Internal-Key")
        return httpx.Response(200, json=[{"id": str(uuid.uuid4()), "filename": "draft.md"}])

    arts = await _client(handler).list_artifacts(gid)
    assert len(arts) == 1
    assert seen["path"] == "/v1/artifacts"
    assert seen["graph_id"] == str(gid)  # org-scoped read keyed on the bound graph
    assert seen["key"] == "k"  # downstream identity headers passed through


async def test_404_is_an_empty_list_not_an_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    # a graph that does not exist OR belongs to another org → nothing landed for this caller
    assert await _client(handler).list_artifacts(uuid.uuid4()) == []


async def test_non_2xx_raises_fail_closed() -> None:
    from oraclous_execution_engine_service.services.artifacts_client import ArtifactsClientError

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="kaboom")

    with pytest.raises(ArtifactsClientError):
        await _client(handler).list_artifacts(uuid.uuid4())


async def test_transport_error_raises_fail_closed() -> None:
    from oraclous_execution_engine_service.services.artifacts_client import ArtifactsClientError

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kgs unreachable")

    with pytest.raises(ArtifactsClientError):
        await _client(handler).list_artifacts(uuid.uuid4())
