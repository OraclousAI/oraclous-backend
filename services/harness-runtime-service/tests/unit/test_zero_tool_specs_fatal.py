"""Unit: #698 AC6 — a tool capability that yields no callable tool spec fails LOUDLY at load.

The #698 chain was invisible for exactly this reason. A member was bound to an imported MCP tool,
``tool_specs_for`` returned ``[]`` because the descriptor stored no operations (D1), and the run
started anyway — the model was handed an empty tool list, invented an answer, and the step trace
showed a normal-looking completion. The user paid for real tokens to be told nothing was wrong.

A resolved ``kind=tool`` capability that produces zero specs is a broken binding, not an empty one.
It must raise at setup, before the LLM is built and before a single token is spent.

Scoped deliberately: only descriptors that declare ``kind: "tool"`` are checked. Knowledge, graph
and retriever bindings legitimately emit no ``ToolSpec`` — they are bound as configuration, not as
callable functions — and making those fatal would break every team that uses one.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionError,
    HarnessExecutionService,
)
from oraclous_ohm.manifest import OHMCapability, OHMManifest, OHMMetadata, OHMRuntime
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()

# An imported MCP tool as it exists TODAY: kind=tool, and no operations at all (the D1 defect).
_TOOL_WITH_NO_OPERATIONS = {
    "kind": "tool",
    "metadata": {"name": "github-mcp-pull_request_read"},
    "spec": {
        "type": "mcp",
        "server_url": "https://93.184.216.34/mcp",
        "tool_name": "pull_request_read",
    },
}

# The same tool once D1 lands — one operation, so one callable spec.
_TOOL_WITH_AN_OPERATION = {
    "kind": "tool",
    "metadata": {"name": "github-mcp-pull_request_read"},
    "spec": {
        "type": "mcp",
        "server_url": "https://93.184.216.34/mcp",
        "tool_name": "pull_request_read",
        "capabilities": [
            {"name": "pull_request_read", "parameters_schema": {"type": "object", "properties": {}}}
        ],
    },
}

# A non-tool binding that legitimately emits nothing — it must keep working untouched.
_KNOWLEDGE_BINDING = {
    "kind": "knowledge",
    "metadata": {"name": "org-graph"},
    "spec": {"type": "knowledge_graph", "capabilities": []},
}

# A descriptor that declares no kind at all (older first-party rows). Out of scope by design: the
# check keys on an explicit kind=tool, so an unlabelled descriptor is never made fatal.
_UNLABELLED = {"metadata": {"name": "Legacy Thing"}, "spec": {"capabilities": []}}


class _Registry:
    """A registry fake: no instances to reuse, so every binding mints one."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def list_instances(self) -> list[dict[str, Any]]:
        return []

    async def create_instance(
        self, *, capability_id: str, name: str, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        self.created.append({"capability_id": capability_id, "name": name, "config": configuration})
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(
        self, instance_id: uuid.UUID, mappings: dict[str, str]
    ) -> dict[str, Any]:
        return {}


def _service(registry: _Registry) -> HarnessExecutionService:
    return HarnessExecutionService(
        registry=registry,
        broker=None,
        executions=None,
        assignments=None,
        checkpoints=None,
        provenance=None,
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
        memory=None,
    )


def _manifest(binding: str, ref: str) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[OHMCapability(ref=ref, binding=binding, config={})],
        runtime=OHMRuntime(entrypoint=binding),
    )


_ORG_REF = "org:00000000-0000-0000-0000-0000000000aa/github-mcp-pull_request_read"


async def test_a_tool_capability_with_no_operations_raises_at_setup() -> None:
    """The core of AC6: the run stops before the LLM is built, so no tokens are spent."""
    registry = _Registry()
    resolved = {"github-mcp": {"id": "cap-mcp", "descriptor": _TOOL_WITH_NO_OPERATIONS}}
    with pytest.raises(HarnessExecutionError):
        await _service(registry)._materialise(_manifest("github-mcp", _ORG_REF), resolved)


async def test_the_failure_names_the_binding_the_user_must_fix() -> None:
    """A user who sees this must know WHICH tool is broken — an opaque error is a support ticket."""
    resolved = {"github-mcp": {"id": "cap-mcp", "descriptor": _TOOL_WITH_NO_OPERATIONS}}
    with pytest.raises(HarnessExecutionError) as err:
        await _service(_Registry())._materialise(_manifest("github-mcp", _ORG_REF), resolved)
    assert "github-mcp" in str(err.value)


async def test_a_tool_capability_with_one_operation_materialises_normally() -> None:
    registry = _Registry()
    resolved = {"github-mcp": {"id": "cap-mcp", "descriptor": _TOOL_WITH_AN_OPERATION}}
    instance_by_binding, tool_specs = await _service(registry)._materialise(
        _manifest("github-mcp", _ORG_REF), resolved
    )
    assert instance_by_binding["github-mcp"] is not None
    assert [s.name for s in tool_specs] == ["github-mcp__pull_request_read"]


async def test_a_knowledge_binding_emitting_no_specs_is_not_fatal() -> None:
    """The scoping guard. A knowledge binding is configuration, not a callable function."""
    resolved = {"org-graph": {"id": "cap-kg", "descriptor": _KNOWLEDGE_BINDING}}
    _, tool_specs = await _service(_Registry())._materialise(
        _manifest("org-graph", "core/knowledge-graph@1.0.0"), resolved
    )
    assert tool_specs == []


async def test_a_descriptor_declaring_no_kind_is_not_fatal() -> None:
    """Out of scope by design — the check requires an explicit ``kind: "tool"``, so existing
    first-party rows that never declared one keep loading exactly as they do today."""
    resolved = {"legacy": {"id": "cap-legacy", "descriptor": _UNLABELLED}}
    _, tool_specs = await _service(_Registry())._materialise(
        _manifest("legacy", "core/legacy-thing@1.0.0"), resolved
    )
    assert tool_specs == []
