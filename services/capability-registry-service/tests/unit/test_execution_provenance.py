"""Unit: §3.7 provenance on every registry dispatch AND every pre-dispatch refusal (#826).

The 24 August ruling: ``capability-registry-service`` has ZERO collector usage today, and a denied
invoke is exactly the event an audit trail exists for — the four refusal gates in ``execute_sync``
(instance not found, pending admin approval, undeclared operation, no executor, credential miss),
raised BEFORE ``create_queued``'s first DB write, currently emit nothing. This pins:

* ``ToolExecutionService`` takes a NEW non-optional ``provenance`` constructor kwarg.
* a successful dispatch emits ONE ``capability.invoke`` record with ``input_hash``/``output_hash``
  set;
* a failed executor RESULT (``result.success is False``, no exception) emits ``outcome="failed"``;
* EACH refusal gate emits a ``capability.refused`` record, with the machine-readable reason as both
  ``outcome`` and ``context["error_code"]``, BEFORE the exception propagates;
* a refusal never reaches the ``executions`` table — that table is operational data, not provenance
  (24 August ruling §2).

RED until ``ToolExecutionService`` accepts ``provenance`` and emits through it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_capability_registry_service.domain.operations import UNSUPPORTED_OPERATION
from oraclous_capability_registry_service.domain.plugins.builtin import (
    ManifestValidatePlugin,
    WebResearchPlugin,
)
from oraclous_capability_registry_service.models.enums import ExecutionStatus
from oraclous_capability_registry_service.schema.execution_schema import ExecuteRequest
from oraclous_capability_registry_service.services.credential_client import (
    CredentialResolutionError,
)
from oraclous_capability_registry_service.services.instance_manager import InstanceNotFoundError
from oraclous_capability_registry_service.services.tool_execution_service import (
    ExecutionNotReadyError,
    ToolExecutionService,
)

pytestmark = [pytest.mark.unit, pytest.mark.audit]

_ORG = uuid.uuid4()
_CAP = uuid.uuid4()
_INST = uuid.uuid4()
_USER = uuid.uuid4()
_EXEC = uuid.uuid4()


class _FakeProvenance:
    """Recording double in the ``test_job_service.py`` style — captures the whole record."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    async def emit(self, record: Any) -> None:  # noqa: ANN401
        self.records.append(record)


class _SpyExecutions:
    """Proves a refusal never reaches the operational ``executions`` store (24 Aug ruling §2)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create_queued(self, **_k: object) -> Any:
        self.calls.append("create_queued")
        return SimpleNamespace(id=_EXEC)

    async def finalize(self, **kwargs: Any) -> Any:
        self.calls.append("finalize")
        return SimpleNamespace(
            id=_EXEC,
            organisation_id=_ORG,
            instance_id=_INST,
            capability_id=_CAP,
            user_id=_USER,
            status=kwargs["status"],
            output_data=kwargs.get("output_data"),
            credential_refs=[],
            error_message=kwargs.get("error_message"),
            error_type=kwargs.get("error_type"),
            credits_consumed=0,
            processing_time_ms=0,
            created_at=None,
        )


class _FakeInstances:
    async def get_by_id(self, instance_id: uuid.UUID, organisation_id: uuid.UUID) -> Any:  # noqa: ARG002
        return SimpleNamespace(
            id=_INST,
            capability_id=_CAP,
            organisation_id=_ORG,
            credential_mappings={},
            configuration={},
            settings={},
        )

    async def record_execution(self, *_a: object, **_k: object) -> None:
        return None


class _MissingInstance:
    async def get_by_id(self, instance_id: uuid.UUID, organisation_id: uuid.UUID) -> Any:  # noqa: ARG002
        return None


class _DescriptorCaps:
    """A capability repository stub returning a fixed descriptor + status."""

    def __init__(self, descriptor: dict[str, Any], *, status: str = "active") -> None:
        self._descriptor = descriptor
        self._status = status

    async def get_by_id(self, capability_id: uuid.UUID, organisation_id: uuid.UUID) -> Any:  # noqa: ARG002
        return SimpleNamespace(
            organisation_id=_ORG, status=self._status, descriptor=self._descriptor
        )

    async def list_by_org(self, organisation_id: uuid.UUID) -> list[Any]:  # noqa: ARG002
        return []

    async def list_by_kind(self, organisation_id: uuid.UUID, kind: Any) -> list[Any]:  # noqa: ARG002
        return []


class _MissBroker:
    async def resolve(self, *, organisation_id, user_id, requirement, credential_id=None) -> Any:  # noqa: ANN001, ARG002
        raise CredentialResolutionError(
            "no credential is mapped for requirement api_key", error_code="credential_not_mapped"
        )


_PENDING_MCP_DESCRIPTOR = {
    "kind": "tool",
    "metadata": {"name": "acme/do"},
    "spec": {"type": "mcp", "server_url": "https://e.example.com", "tool_name": "do"},
}

_DECLARES_ONE_OP_DESCRIPTOR = {
    "kind": "tool",
    "metadata": {"name": "acme/limited"},
    "spec": {"type": "function", "capabilities": [{"name": "allowed_op"}]},
}

_NO_EXECUTOR_DESCRIPTOR = {
    "kind": "tool",
    "metadata": {"name": "acme/executorless"},
    "spec": {
        "type": "API",
        "capabilities": [{"name": "read_nothing"}],
        "credential_requirements": [],
    },
}

_WEB_RESEARCH_DESCRIPTOR = {
    "id": WebResearchPlugin.plugin_id(),
    "kind": "tool",
    "metadata": {"name": "core/web-research"},
    "spec": {
        "type": "web_research",
        "capabilities": [{"name": "search"}],
        "credential_requirements": [
            {"type": "api_key", "provider": "web_search", "required": True},
        ],
    },
}


def test_constructor_requires_provenance() -> None:
    """``ToolExecutionService`` takes a NEW non-optional ``provenance`` kwarg."""
    with pytest.raises(TypeError):
        ToolExecutionService(  # type: ignore[call-arg]
            instances=_FakeInstances(),
            capabilities=_DescriptorCaps(ManifestValidatePlugin.descriptor()),
            executions=_SpyExecutions(),
            broker=None,
        )


async def test_successful_execution_emits_one_invoke_record_with_both_hashes() -> None:
    from oraclous_substrate.provenance import hash_payload  # function-local, per the seam rule

    prov = _FakeProvenance()
    svc = ToolExecutionService(
        instances=_FakeInstances(),
        capabilities=_DescriptorCaps(ManifestValidatePlugin.descriptor()),
        executions=_SpyExecutions(),
        broker=None,
        provenance=prov,  # type: ignore[call-arg]
    )
    input_data = {"draft": {"members": []}}
    out = await svc.execute_sync(
        instance_id=_INST,
        body=ExecuteRequest(input_data=input_data),
        organisation_id=_ORG,
        user_id=_USER,
    )
    assert out.status == ExecutionStatus.SUCCESS
    assert len(prov.records) == 1
    rec = prov.records[0]
    assert rec.action == "capability.invoke"
    assert rec.resource == f"tool_instance:{_INST}"
    assert rec.outcome == "succeeded"
    assert rec.organisation_id == str(_ORG)
    assert rec.principal == str(_USER)
    # Pinned, not merely "is not None" — a hash of the WRONG object (the whole request body, the
    # instance id, an empty dict) must fail this, not slip through.
    assert rec.input_hash == hash_payload(input_data)
    assert rec.output_hash == hash_payload(out.output_data)


async def test_input_hash_differs_for_a_different_input_payload() -> None:
    """Proves the ``input_hash`` assertion above has teeth: two different inputs hash to two
    different values — an implementation that hashes the wrong object (e.g. always the instance id,
    or an empty dict) cannot pass both this test and the pinned-value assertion above."""
    from oraclous_substrate.provenance import hash_payload  # function-local, per the seam rule

    prov = _FakeProvenance()
    svc = ToolExecutionService(
        instances=_FakeInstances(),
        capabilities=_DescriptorCaps(ManifestValidatePlugin.descriptor()),
        executions=_SpyExecutions(),
        broker=None,
        provenance=prov,  # type: ignore[call-arg]
    )
    first_input = {"draft": {"members": []}}
    second_input = {"draft": {"members": [{"id": "solo"}]}}
    await svc.execute_sync(
        instance_id=_INST,
        body=ExecuteRequest(input_data=first_input),
        organisation_id=_ORG,
        user_id=_USER,
    )
    await svc.execute_sync(
        instance_id=_INST,
        body=ExecuteRequest(input_data=second_input),
        organisation_id=_ORG,
        user_id=_USER,
    )
    assert len(prov.records) == 2
    assert prov.records[0].input_hash == hash_payload(first_input)
    assert prov.records[1].input_hash == hash_payload(second_input)
    assert prov.records[0].input_hash != prov.records[1].input_hash


async def test_failed_executor_result_emits_failed_outcome() -> None:
    """A failed executor RESULT (no exception raised) still records the input hash."""
    from oraclous_substrate.provenance import hash_payload  # function-local, per the seam rule

    prov = _FakeProvenance()
    svc = ToolExecutionService(
        instances=_FakeInstances(),
        capabilities=_DescriptorCaps(ManifestValidatePlugin.descriptor()),
        executions=_SpyExecutions(),
        broker=None,
        provenance=prov,  # type: ignore[call-arg]
    )
    out = await svc.execute_sync(
        instance_id=_INST,
        # no "draft" key -> ManifestValidateConnector returns ExecutionResult(success=False, ...)
        body=ExecuteRequest(input_data={}),
        organisation_id=_ORG,
        user_id=_USER,
    )
    assert out.status == ExecutionStatus.FAILED
    assert len(prov.records) == 1
    rec = prov.records[0]
    assert rec.action == "capability.invoke"
    assert rec.outcome == "failed"
    assert rec.input_hash == hash_payload({})
    # output_hash MAY be None on a failed result — not asserted either way.


async def test_instance_not_found_emits_a_refused_record_before_raising() -> None:
    prov = _FakeProvenance()
    execs = _SpyExecutions()
    svc = ToolExecutionService(
        instances=_MissingInstance(),
        capabilities=_DescriptorCaps(ManifestValidatePlugin.descriptor()),
        executions=execs,
        broker=None,
        provenance=prov,  # type: ignore[call-arg]
    )
    with pytest.raises(InstanceNotFoundError):
        await svc.execute_sync(
            instance_id=_INST,
            body=ExecuteRequest(input_data={}),
            organisation_id=_ORG,
            user_id=_USER,
        )
    assert execs.calls == []
    assert len(prov.records) == 1
    rec = prov.records[0]
    assert rec.action == "capability.refused"
    assert rec.resource == f"tool_instance:{_INST}"
    assert rec.outcome == "instance_not_found"
    # CARRIES the reason — not the record's only key, so an implementer who adds e.g. the
    # instance id to context does not have to edit this test to stay green.
    assert rec.context is not None
    assert rec.context["error_code"] == "instance_not_found"


def _refusal_case(name: str) -> tuple[Any, Any, dict[str, Any], str]:
    """Returns (capabilities_repo, broker, input_data, expected_error_code) for one refusal gate."""
    if name == "pending_approval":
        return (
            _DescriptorCaps(_PENDING_MCP_DESCRIPTOR, status="pending_approval"),
            None,
            {},
            "pending_approval",
        )
    if name == "unsupported_operation":
        return (
            _DescriptorCaps(_DECLARES_ONE_OP_DESCRIPTOR),
            None,
            {"operation": "not_allowed"},
            UNSUPPORTED_OPERATION,
        )
    if name == "no_executor":
        return (
            _DescriptorCaps(_NO_EXECUTOR_DESCRIPTOR),
            None,
            {},
            "no_executor",
        )
    if name == "needs_credential":
        return (
            _DescriptorCaps(_WEB_RESEARCH_DESCRIPTOR),
            _MissBroker(),
            {"operation": "search", "query": "x"},
            "credential_not_mapped",
        )
    raise ValueError(name)  # pragma: no cover — parametrize typo guard


@pytest.mark.parametrize(
    "case", ["pending_approval", "unsupported_operation", "no_executor", "needs_credential"]
)
async def test_each_execution_not_ready_gate_emits_a_refused_record_before_raising(
    case: str,
) -> None:
    capabilities, broker, input_data, expected_error_code = _refusal_case(case)
    prov = _FakeProvenance()
    execs = _SpyExecutions()
    svc = ToolExecutionService(
        instances=_FakeInstances(),
        capabilities=capabilities,
        executions=execs,
        broker=broker,
        provenance=prov,  # type: ignore[call-arg]
    )
    with pytest.raises(ExecutionNotReadyError) as exc:
        await svc.execute_sync(
            instance_id=_INST,
            body=ExecuteRequest(input_data=input_data),
            organisation_id=_ORG,
            user_id=_USER,
        )
    assert exc.value.error_code == expected_error_code

    # A refusal is NOT operational data — the executions table must never be touched (§2 ruling).
    assert execs.calls == []

    assert len(prov.records) == 1
    rec = prov.records[0]
    assert rec.action == "capability.refused"
    assert rec.resource == f"tool_instance:{_INST}"
    assert rec.outcome == expected_error_code
    # CARRIES the reason — not the record's only key (same relaxation as the instance-miss case).
    assert rec.context is not None
    assert rec.context["error_code"] == expected_error_code
