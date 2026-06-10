"""Unit: the supply-chain HITL execution gate — a pending MCP tool is not executable (R6 MCP)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from oraclous_capability_registry_service.schema.execution_schema import ExecuteRequest
from oraclous_capability_registry_service.services.tool_execution_service import (
    ExecutionNotReadyError,
    ToolExecutionService,
)

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_CAP = uuid.uuid4()
_INST = uuid.uuid4()


class _FakeInstances:
    async def get_by_id(self, instance_id, organisation_id):  # noqa: ANN001, ANN202, ARG002
        return SimpleNamespace(id=_INST, capability_id=_CAP, organisation_id=_ORG)


class _FakeCaps:
    def __init__(self, *, status: str) -> None:
        self._status = status

    async def get_by_id(self, capability_id, organisation_id):  # noqa: ANN001, ANN202, ARG002
        return SimpleNamespace(
            organisation_id=_ORG,
            status=self._status,
            descriptor={
                "kind": "tool",
                "metadata": {"name": "acme/do"},
                "spec": {"type": "mcp", "server_url": "https://e.example.com", "tool_name": "do"},
            },
        )


def _svc(status: str) -> ToolExecutionService:
    # executions + broker are never reached — the gate raises before them.
    return ToolExecutionService(
        instances=_FakeInstances(),
        capabilities=_FakeCaps(status=status),
        executions=None,
        broker=None,
    )


async def test_a_pending_mcp_tool_is_refused_before_execution() -> None:
    with pytest.raises(ExecutionNotReadyError) as exc:
        await _svc("pending_approval").execute_sync(
            instance_id=_INST,
            body=ExecuteRequest(input_data={"x": 1}),
            organisation_id=_ORG,
            user_id=uuid.uuid4(),
        )
    assert exc.value.error_code == "pending_approval"


async def test_an_active_mcp_tool_passes_the_gate() -> None:
    # status=active clears the HITL gate; it then proceeds (and fails later on the unreachable net
    # / missing executions — NOT on the approval gate). We only assert the gate did NOT fire.
    with pytest.raises(Exception) as exc:  # noqa: B017, PT011
        await _svc("active").execute_sync(
            instance_id=_INST,
            body=ExecuteRequest(input_data={"x": 1}),
            organisation_id=_ORG,
            user_id=uuid.uuid4(),
        )
    assert not (
        isinstance(exc.value, ExecutionNotReadyError) and exc.value.error_code == "pending_approval"
    )
