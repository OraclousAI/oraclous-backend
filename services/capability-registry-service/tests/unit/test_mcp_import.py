"""Unit: McpImportService — discover → pending descriptors, egress, approve, no-leak (R6 MCP)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from oraclous_capability_registry_service.services.mcp_import_service import (
    ACTIVE,
    PENDING,
    McpEgressBlocked,
    McpImportError,
    McpImportService,
)

pytestmark = pytest.mark.unit

_PUB = "https://93.184.216.34/mcp"  # a literal PUBLIC ip → egress allowed without a DNS lookup


class _FakeCaps:
    def __init__(self) -> None:
        self.created: list = []
        self.statuses: dict = {}

    async def create(self, *, organisation_id, kind, descriptor, status="active"):  # noqa: ANN001, ANN202
        row = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            kind=kind,
            descriptor=descriptor,
            status=status,
        )
        self.created.append(row)
        return row

    async def set_status(self, *, descriptor_id, organisation_id, status):  # noqa: ANN001, ANN202
        self.statuses[descriptor_id] = status
        return True


def _svc(caps: _FakeCaps, handler) -> McpImportService:  # noqa: ANN001
    return McpImportService(capabilities=caps, transport=httpx.MockTransport(handler))


async def test_import_registers_discovered_tools_as_pending_approval() -> None:
    caps = _FakeCaps()
    handler = lambda _r: httpx.Response(  # noqa: E731
        200,
        json={"result": {"tools": [{"name": "do_a", "description": "A"}, {"name": "do_b"}]}},
    )
    created = await _svc(caps, handler).import_server(
        organisation_id=uuid.uuid4(), server_url=_PUB, label="acme"
    )
    assert len(created) == 2 and all(r.status == PENDING for r in created)
    assert created[0].descriptor["spec"] == {
        "type": "mcp",
        "server_url": _PUB,
        "tool_name": "do_a",
    }
    assert created[0].descriptor["metadata"]["name"] == "acme/do_a"


async def test_import_blocks_an_internal_url_before_any_call() -> None:
    called = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    with pytest.raises(McpEgressBlocked):
        await _svc(_FakeCaps(), handler).import_server(
            organisation_id=uuid.uuid4(), server_url="http://169.254.169.254/", label="x"
        )
    assert called["n"] == 0  # never reached the network


@pytest.mark.parametrize("body", [[1, 2], {"result": "x"}, {"no": "result"}, "bare"])
async def test_a_malformed_tools_list_is_a_generic_error(body: object) -> None:
    with pytest.raises(McpImportError):
        await _svc(_FakeCaps(), lambda _r: httpx.Response(200, json=body)).import_server(
            organisation_id=uuid.uuid4(), server_url=_PUB, label="x"
        )


async def test_a_non_200_never_leaks_the_servers_body() -> None:
    with pytest.raises(McpImportError) as exc:
        await _svc(
            _FakeCaps(), lambda _r: httpx.Response(500, text="SECRET stack trace")
        ).import_server(organisation_id=uuid.uuid4(), server_url=_PUB, label="x")
    assert "SECRET" not in str(exc.value)


async def test_approve_flips_status_to_active() -> None:
    caps = _FakeCaps()
    tid, org = uuid.uuid4(), uuid.uuid4()
    ok = await McpImportService(capabilities=caps).approve(descriptor_id=tid, organisation_id=org)
    assert ok and caps.statuses[tid] == ACTIVE


def test_status_for_forces_pending_for_any_mcp_descriptor() -> None:
    # the side-door fix: an MCP tool is pending_approval at creation no matter the path or a passed
    # status (you cannot register an MCP tool directly as active and dodge the HITL gate).
    from oraclous_capability_registry_service.repositories.capability_repository import status_for

    assert status_for({"spec": {"type": "mcp"}}, "active") == "pending_approval"
    assert status_for({"spec": {"type": "mcp"}}, "pending_approval") == "pending_approval"
    # a non-MCP descriptor keeps the requested status (built-ins / first-party stay active)
    assert status_for({"spec": {"type": "database"}}, "active") == "active"
    assert status_for({}, "active") == "active"
