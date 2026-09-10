"""Unit (#1004, item 1): the registry validates ``operation`` against the INSTANCE's descriptor.

#956 closed the model→operation channel in the harness runtime. The registry itself still trusted
whatever ``input_data["operation"]`` arrived: ``execute_sync`` handed ``body.input_data`` straight
to ``executor.execute``, and only the connector's own hardcoded whitelist stood between a caller
and any operation that connector class happens to implement. A direct
``POST /api/v1/instances/{id}/execute`` — the same route the engine's scheduled adopted-tool worker
drives — was never checked against what the instance's descriptor DECLARES.

Ruled by the owner (recorded on #1004): the two services must agree independently.

* ``descriptor["spec"]["capabilities"]`` is the declared set — one object per operation, each with
  a ``name``. Every first-party tool writes it (``plugins/builtin.py``), and an MCP import writes
  exactly one (the approved server tool's name), so the gate covers the imported path too.
* An ``operation`` that is present and NOT declared fails closed, before any executor runs.
* An ``operation`` that is absent is NOT a refusal: the connector's own default stands
  (``postgresql`` → ``query``, ``github`` → ``list_files``). Shipped callers omit the key.
* The refusal carries the coded token ``unsupported_operation`` and a message bounded by
  ``TOOL_ERROR_CHARS`` — the caller-supplied value is never echoed unbounded into a persisted
  error a model reads.

RED until the #1004 ``[impl]`` lands: today every operation reaches the executor.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_capability_registry_service.schema.execution_schema import ExecuteRequest
from oraclous_capability_registry_service.services.tool_execution_service import (
    ExecutionNotReadyError,
    ToolExecutionService,
)

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.tool_dispatch]

_ORG = uuid.uuid4()
_CAP = uuid.uuid4()
_INST = uuid.uuid4()
_USER = uuid.uuid4()

#: The coded token the refusal carries, in the registry's own closed vocabulary — the same shape
#: as the sibling gates ``pending_approval`` / ``no_executor`` on this call, and the shape the
#: harness's ``registry_client._CODE_TOKEN`` accepts across the leak boundary (#692).
_CODE = "unsupported_operation"


def _bound() -> int:
    """``TOOL_ERROR_CHARS`` — one number for every path that names a bad operation (#956)."""
    from oraclous_capability_registry_service.domain.executors.base import TOOL_ERROR_CHARS

    return int(TOOL_ERROR_CHARS)


# A first-party reader declaring exactly two read operations. ``drop_table`` is deliberately NOT
# declared: it stands for any operation the connector class may implement that this descriptor
# never offered.
def _descriptor(
    *, capabilities: list[dict[str, Any]] | None, spec_type: str = "DATABASE"
) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": spec_type}
    if capabilities is not None:
        spec["capabilities"] = capabilities
    return {"kind": "tool", "metadata": {"name": "Test Reader"}, "spec": spec}


_TWO_OPS = [
    {"name": "list_tables", "description": "List tables"},
    {"name": "query", "description": "Run a query"},
]


class _FakeInstances:
    def __init__(self) -> None:
        self.recorded: list[Any] = []

    async def get_by_id(self, instance_id, organisation_id):  # noqa: ANN001, ANN202, ARG002
        return SimpleNamespace(
            id=_INST,
            capability_id=_CAP,
            organisation_id=_ORG,
            credential_mappings={},
            configuration={},
            settings={},
        )

    async def record_execution(self, *args: Any, **kwargs: Any) -> None:
        self.recorded.append((args, kwargs))


class _FakeCaps:
    def __init__(self, descriptor: dict[str, Any]) -> None:
        self._descriptor = descriptor

    async def get_by_id(self, capability_id, organisation_id):  # noqa: ANN001, ANN202, ARG002
        return SimpleNamespace(organisation_id=_ORG, status="active", descriptor=self._descriptor)


class _PastTheGate(Exception):
    """Raised by the fakes that sit BEYOND the gate. Seeing it means execution proceeded."""


class _TripwireExecutions:
    """Creating the provenance row is the first thing that happens after the pre-flight gates."""

    async def create_queued(self, **kwargs: Any) -> Any:  # noqa: ANN401
        raise _PastTheGate("a provenance row was created")


class _TripwireBroker:
    async def resolve(self, **kwargs: Any) -> Any:  # noqa: ANN401
        raise _PastTheGate("a credential was resolved")


def _svc(descriptor: dict[str, Any]) -> ToolExecutionService:
    return ToolExecutionService(
        instances=_FakeInstances(),
        capabilities=_FakeCaps(descriptor),
        executions=_TripwireExecutions(),
        broker=_TripwireBroker(),
    )


async def _execute(descriptor: dict[str, Any], input_data: dict[str, Any]) -> Any:  # noqa: ANN401
    return await _svc(descriptor).execute_sync(
        instance_id=_INST,
        body=ExecuteRequest(input_data=input_data),
        organisation_id=_ORG,
        user_id=_USER,
    )


def _refusal(exc: ExecutionNotReadyError) -> None:
    assert exc.error_code == _CODE, f"the refusal is not coded {_CODE!r}"


# --- an undeclared operation fails closed, before any executor -----------------------------------


async def test_an_undeclared_operation_is_refused_before_the_executor() -> None:
    """The defect itself: today ``drop_table`` reaches ``executor.execute`` and only the
    connector's own whitelist decides. The tripwire fakes prove nothing beyond the gate ran."""
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(_descriptor(capabilities=_TWO_OPS), {"operation": "drop_table"})

    _refusal(ei.value)


async def test_a_declared_operation_passes_the_gate() -> None:
    """The regression guard. This synthetic descriptor has no registered executor, so passing the
    declared-operation gate lands on the NEXT pre-flight gate (``no_executor``) — pinning both that
    the gate let a declared name through and that it runs BEFORE the executor lookup."""
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(
            _descriptor(capabilities=_TWO_OPS), {"operation": "query", "query": "SELECT 1"}
        )

    assert ei.value.error_code == "no_executor", (
        "a DECLARED operation did not reach the executor lookup"
    )


async def test_an_absent_operation_is_not_a_refusal() -> None:
    """A caller that chose nothing gets the connector's own hardcoded default (``manifest-validate``
    / ``script-ingestion`` / the team-run spec all call ``/execute`` with no ``operation``). That
    default is connector CODE, not caller input, so it is not the #956 threat surface."""
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(_descriptor(capabilities=_TWO_OPS), {"draft": {"x": 1}})

    assert ei.value.error_code == "no_executor", (
        "a call with NO operation key was refused — that is a regression, not a gate"
    )


async def test_an_operation_on_a_descriptor_that_declares_none_is_refused() -> None:
    """Fail-closed (CLAUDE.md §3.5): nothing declares this operation, so nothing authorises it."""
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(_descriptor(capabilities=None), {"operation": "query"})

    _refusal(ei.value)


async def test_an_operation_on_an_empty_declared_set_is_refused() -> None:
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(_descriptor(capabilities=[]), {"operation": "query"})

    _refusal(ei.value)


async def test_the_match_is_exact_not_case_folded() -> None:
    """Fail-closed means no normalisation on the registry side either: ``QUERY`` is not ``query``.
    The connectors dispatch on the exact lowercase name."""
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(_descriptor(capabilities=_TWO_OPS), {"operation": "QUERY"})

    _refusal(ei.value)


@pytest.mark.parametrize("value", [["query"], {"name": "query"}, 1, True])
async def test_a_non_string_operation_is_refused(value: object) -> None:
    """A type the key cannot legitimately hold is not a loophole into "absent"."""
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(_descriptor(capabilities=_TWO_OPS), {"operation": value})

    _refusal(ei.value)


async def test_a_malformed_declared_entry_never_widens_the_set() -> None:
    """``spec.capabilities`` is JSONB — a row can hold anything. A non-dict entry, or one with no
    ``name``, declares no operation; it must not become a wildcard."""
    ragged = [{"description": "no name"}, "query", None, {"name": "list_tables"}]
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(_descriptor(capabilities=ragged), {"operation": "query"})

    _refusal(ei.value)


# --- the refusal is bounded ----------------------------------------------------------------------


async def test_the_refusal_message_is_bounded_and_does_not_echo_the_value() -> None:
    """The 409 body carries ``str(exc)``; the harness turns it into words a MODEL reads. An
    unbounded ``f"…'{operation}'"`` would be a general echo channel for arbitrary caller text —
    the same class #956 closed on the harness side and #697 bounded for a tool's own words."""
    injected = "z" * 4000
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(_descriptor(capabilities=_TWO_OPS), {"operation": injected})

    detail = str(ei.value)
    _refusal(ei.value)
    assert len(detail) <= _bound(), (
        f"the refusal message is longer than TOOL_ERROR_CHARS ({_bound()})"
    )
    assert injected not in detail


async def test_the_refusal_detail_carries_no_caller_value_either() -> None:
    """``ExecutionNotReadyError.detail`` is spread into the 409 body at the TOP level by the app
    factory, so it is a second surface with the same rule."""
    injected = "y" * 4000
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(_descriptor(capabilities=_TWO_OPS), {"operation": injected})

    assert injected not in repr(ei.value.detail)


# --- the imported (MCP) path is validated, not exempt ---------------------------------------------


async def test_an_mcp_instance_runs_only_the_approved_tool_name() -> None:
    """An MCP import is NOT dynamic at execute time: ``McpImportService.import_server`` writes
    exactly one operation — the discovered tool's name — and an org admin approves THAT descriptor.
    So the declared set of an imported instance is the single tool the admin approved, and the gate
    is meaningful rather than an exemption."""
    mcp = {
        "kind": "tool",
        "metadata": {"name": "acme/create_issue"},
        "spec": {
            "type": "mcp",
            "server_url": "https://mcp.example.com",
            "tool_name": "create_issue",
            "capabilities": [{"name": "create_issue", "description": "Open an issue"}],
        },
    }
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _execute(mcp, {"operation": "delete_repository", "repo": "a/b"})

    _refusal(ei.value)


async def test_an_mcp_instances_own_tool_name_passes_the_gate() -> None:
    mcp = {
        "kind": "tool",
        "metadata": {"name": "acme/create_issue"},
        "spec": {
            "type": "mcp",
            "server_url": "https://mcp.example.com",
            "tool_name": "create_issue",
            "capabilities": [{"name": "create_issue", "description": "Open an issue"}],
        },
    }
    # an mcp descriptor DOES have a registered executor, so passing the gate reaches the tripwire
    # that stands in for the provenance row — proof the call proceeded past every pre-flight gate.
    with pytest.raises(_PastTheGate):
        await _execute(mcp, {"operation": "create_issue", "title": "x"})
