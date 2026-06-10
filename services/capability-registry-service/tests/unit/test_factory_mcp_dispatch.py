"""Unit: the executor factory dispatch — built-ins by id, an imported MCP tool by spec.type."""

from __future__ import annotations

import pytest
from oraclous_capability_registry_service.domain.connectors.mcp import McpToolExecutor
from oraclous_capability_registry_service.domain.executors.factory import (
    NoExecutorError,
    create_executor,
    has_executor,
)

pytestmark = pytest.mark.unit

_MCP = {
    "id": "an-imported-tool-uuid",  # NOT a built-in plugin id
    "spec": {"type": "mcp", "server_url": "https://mcp.example.com/rpc", "tool_name": "t"},
}


def test_an_mcp_descriptor_dispatches_to_the_mcp_executor() -> None:
    assert has_executor(_MCP) is True
    assert isinstance(create_executor(_MCP), McpToolExecutor)


def test_a_descriptor_with_no_executor_raises() -> None:
    bogus = {"id": "not-a-plugin", "spec": {"type": "something-else"}}
    assert has_executor(bogus) is False
    with pytest.raises(NoExecutorError):
        create_executor(bogus)
