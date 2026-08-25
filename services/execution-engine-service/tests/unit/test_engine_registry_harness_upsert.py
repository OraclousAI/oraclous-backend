"""The engine's registry client learns to file a generated agent (#695).

A compiled team member and a console-built agent are the same object: an ``OHMManifest`` with
``metadata.kind = "agent"``, produced by the same ``build_subharness``. The console builder POSTs
its descriptor to the registry and keeps the returned id as the agent's ``manifest_ref``. The
compiler files nothing, so its 14 agents were unlistable, uneditable, unbindable, and died with
the run — ``/app/agents`` was empty because nothing had ever been written for it to read.

Find-or-refresh, never blind-insert (the #698 precedent: a re-import refreshes an MCP server's
tools rather than duplicating them). The registry's ``create`` inserts a row keyed on
``descriptor_id``, so a blind second POST is a primary-key conflict, not an update. Hence:
GET the id → 200 means PUT, 404 means POST.

RED until the [impl] adds ``upsert_harness``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from oraclous_execution_engine_service.services.registry_client import (
    RegistryClient,
    RegistryClientError,
    RegistryRejected,
)

pytestmark = pytest.mark.unit

_CAP_ID = uuid.UUID("0f0e0d0c-0b0a-0908-0706-050403020100")


def _descriptor(role: str = "editor", cap_id: uuid.UUID = _CAP_ID) -> dict[str, Any]:
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(cap_id),
            "name": role,
            "kind": "agent",
            "owner_organization_id": str(uuid.uuid4()),
        },
        "capabilities": [{"ref": "core/graph-ingest@1.0.0", "binding": "graph-ingest"}],
    }


class _Recorder:
    """Captures every request so a test can assert the METHOD, which is the whole contract here."""

    def __init__(self, *, exists: bool) -> None:
        self.exists = exists
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            if not self.exists:
                return httpx.Response(404, json={"detail": "capability not found"})
            return httpx.Response(200, json={"id": str(_CAP_ID), "kind": "harness"})
        return httpx.Response(
            201 if request.method == "POST" else 200, json={"id": str(_CAP_ID), "kind": "harness"}
        )

    @property
    def methods(self) -> list[str]:
        return [m for m, _, _ in self.calls]


def _client(handler: Any) -> RegistryClient:
    return RegistryClient(
        "http://registry", headers={"X-Internal-Key": "k"}, transport=httpx.MockTransport(handler)
    )


async def test_a_new_agent_is_created_with_its_own_id_as_the_key() -> None:
    """First save. The descriptor's ``metadata.id`` becomes the capability id, so the two never
    drift and a later refresh can find the row without a search."""
    rec = _Recorder(exists=False)
    client = _client(rec)
    try:
        returned = await client.upsert_harness(_descriptor(), descriptor_id=_CAP_ID)
    finally:
        await client.aclose()
    assert returned == _CAP_ID
    assert rec.methods == ["GET", "POST"]
    post_body = rec.calls[-1][2]
    assert post_body is not None
    assert post_body["kind"] == "harness"
    assert post_body["descriptor_id"] == str(_CAP_ID)
    assert post_body["descriptor"]["metadata"]["name"] == "editor"


async def test_an_existing_agent_is_refreshed_in_place_never_duplicated() -> None:
    """Second save of the same team. One agent, one row, updated content."""
    rec = _Recorder(exists=True)
    client = _client(rec)
    try:
        returned = await client.upsert_harness(_descriptor(), descriptor_id=_CAP_ID)
    finally:
        await client.aclose()
    assert returned == _CAP_ID
    assert rec.methods == ["GET", "PUT"]
    assert "POST" not in rec.methods  # a blind insert would be a primary-key conflict
    assert rec.calls[-1][1].endswith(str(_CAP_ID))


async def test_it_is_registered_as_a_harness_not_as_a_tool() -> None:
    """``/app/agents`` reads ``kind=harness`` for the caller's org. Filed as ``tool`` the agent
    would be invisible there AND would appear on the drafter's tool menu, which #705 filters to
    ``kind=tool`` — a compiled agent offered as a tool to the next compile."""
    rec = _Recorder(exists=False)
    client = _client(rec)
    try:
        await client.upsert_harness(_descriptor(), descriptor_id=_CAP_ID)
    finally:
        await client.aclose()
    assert rec.calls[-1][2]["kind"] == "harness"  # type: ignore[index]


async def test_an_unreachable_registry_raises_the_transport_error() -> None:
    """The caller fails the draft write on this — a half-registered draft is worse than an
    unsaved one — so it must be distinguishable from a rejection."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = _client(boom)
    try:
        with pytest.raises(RegistryClientError):
            await client.upsert_harness(_descriptor(), descriptor_id=_CAP_ID)
    finally:
        await client.aclose()


async def test_a_rejecting_registry_raises_with_the_status_and_detail() -> None:
    """Reachable-but-rejecting is a different fact from unreachable, and the engine reports it
    truthfully rather than as an outage."""

    def rejects(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "capability not found"})
        return httpx.Response(422, json={"detail": "descriptor must be a JSON object"})

    client = _client(rejects)
    try:
        with pytest.raises(RegistryRejected) as exc:
            await client.upsert_harness(_descriptor(), descriptor_id=_CAP_ID)
    finally:
        await client.aclose()
    assert exc.value.status_code == 422
    assert "descriptor" in exc.value.detail


async def test_a_non_404_read_failure_is_not_treated_as_absent() -> None:
    """Fail closed. A 500 on the read means we do not KNOW whether the row exists; POSTing on that
    assumption would conflict, and swallowing it would lose the edit."""

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(503, json={"detail": "registry starting"})
        return httpx.Response(201, json={"id": str(_CAP_ID)})

    client = _client(flaky)
    try:
        with pytest.raises(RegistryClientError):
            await client.upsert_harness(_descriptor(), descriptor_id=_CAP_ID)
    finally:
        await client.aclose()
