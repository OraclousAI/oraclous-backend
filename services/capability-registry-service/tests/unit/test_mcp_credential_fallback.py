"""Unit: #698 D2 — the import-time MCP credential resolves at EXECUTE time.

``McpImportService`` writes ``spec.credential_id`` when an admin imports a hosted server with a
key (#541), and nothing ever reads it again. ``ToolExecutionService`` resolves credentials only
from the *instance*'s ``credential_mappings``, which a member's run never populates for an imported
tool — so the ``tools/call`` goes out anonymous and a hosted server answers 401. The import-time
credential is the org admin's deliberate choice of key for that server; it must be the fallback.

Fail-closed is not relaxed by the fallback: a declared requirement that resolves to nothing still
raises ``needs_credential`` (ADR-039), and the broker call stays org-scoped, so the fallback can
never reach another tenant's credential.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_capability_registry_service.services.credential_client import (
    CredentialResolutionError,
    ResolvedCredential,
)
from oraclous_capability_registry_service.services.tool_execution_service import (
    ExecutionNotReadyError,
    ToolExecutionService,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

_ORG = uuid.uuid4()
_OTHER_ORG = uuid.uuid4()
_CAP = uuid.uuid4()
_INST = uuid.uuid4()
_USER = uuid.uuid4()

_IMPORT_CREDENTIAL = "cred-imported-at-import-time"
_INSTANCE_CREDENTIAL = "cred-mapped-on-the-instance"


def _mcp_descriptor(**spec_extra: Any) -> dict[str, Any]:
    """An approved imported MCP tool, in the shape #698 D1/D2/D4 leave behind."""
    spec: dict[str, Any] = {
        "type": "mcp",
        "server_url": "https://93.184.216.34/mcp",
        "tool_name": "pull_request_read",
        "label": "github-mcp",
        "capabilities": [{"name": "pull_request_read", "parameters_schema": {"type": "object"}}],
        "credential_requirements": [{"type": "api_key", "provider": "mcp", "required": True}],
        **spec_extra,
    }
    return {"kind": "tool", "metadata": {"name": "github-mcp-pull_request_read"}, "spec": spec}


class _FakeInstances:
    def __init__(self, mappings: dict[str, str] | None = None) -> None:
        self._mappings = mappings or {}

    async def get_by_id(self, instance_id: uuid.UUID, organisation_id: uuid.UUID) -> Any:  # noqa: ARG002
        return SimpleNamespace(
            id=_INST,
            capability_id=_CAP,
            organisation_id=_ORG,
            credential_mappings=dict(self._mappings),
        )


class _FakeCaps:
    def __init__(self, descriptor: dict[str, Any], status: str = "active") -> None:
        self._descriptor = descriptor
        self._status = status

    async def get_by_id(self, capability_id: uuid.UUID, organisation_id: uuid.UUID) -> Any:  # noqa: ARG002
        return SimpleNamespace(
            organisation_id=_ORG, status=self._status, descriptor=self._descriptor
        )


class _RecordingBroker:
    """Records every resolve call so the test can assert WHICH credential id was asked for."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise = raise_exc

    async def resolve(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        requirement: dict[str, Any],
        credential_id: str | None = None,
    ) -> ResolvedCredential:
        self.calls.append(
            {
                "organisation_id": organisation_id,
                "user_id": user_id,
                "requirement": requirement,
                "credential_id": credential_id,
            }
        )
        if self._raise is not None:
            raise self._raise
        return ResolvedCredential(credential_type="api_key", payload={"api_key": "resolved-key"})


class _StopAfterCredentials(Exception):
    """Sentinel: credential resolution is done, so the test has everything it needs."""


class _HaltingExecutions:
    """The credential-resolve loop runs BEFORE the execution row is queued. Halting here keeps the
    test on the credential seam and off the network — the real ``tools/call`` is covered by the
    connector unit tests and the integration suite."""

    async def create_queued(self, **_kwargs: Any) -> Any:
        raise _StopAfterCredentials


def _svc(
    descriptor: dict[str, Any], **kwargs: Any
) -> tuple[ToolExecutionService, _RecordingBroker]:
    broker = _RecordingBroker(raise_exc=kwargs.pop("raise_exc", None))
    svc = ToolExecutionService(
        instances=_FakeInstances(kwargs.pop("mappings", None)),
        capabilities=_FakeCaps(descriptor, kwargs.pop("status", "active")),
        executions=_HaltingExecutions(),
        broker=broker,
    )
    return svc, broker


async def _execute(svc: ToolExecutionService, organisation_id: uuid.UUID = _ORG) -> Any:
    from oraclous_capability_registry_service.schema.execution_schema import ExecuteRequest

    return await svc.execute_sync(
        instance_id=_INST,
        body=ExecuteRequest(input_data={"operation": "pull_request_read"}),
        organisation_id=organisation_id,
        user_id=_USER,
    )


async def _execute_to_the_credential_seam(
    svc: ToolExecutionService, organisation_id: uuid.UUID = _ORG
) -> None:
    """Run until credentials are resolved. Anything else raising here is a real failure."""
    with pytest.raises(_StopAfterCredentials):
        await _execute(svc, organisation_id=organisation_id)


async def test_the_import_credential_is_used_when_the_instance_maps_nothing() -> None:
    """The defect itself: a member's run has no mapping, so today the call goes out anonymous."""
    svc, broker = _svc(_mcp_descriptor(credential_id=_IMPORT_CREDENTIAL), mappings={})
    await _execute_to_the_credential_seam(svc)
    assert broker.calls, "the broker was never asked to resolve a credential"
    assert broker.calls[0]["credential_id"] == _IMPORT_CREDENTIAL


async def test_the_instance_mapping_wins_over_the_import_credential() -> None:
    """A member who mapped their own key must keep using it — the fallback is a fallback only."""
    svc, broker = _svc(
        _mcp_descriptor(credential_id=_IMPORT_CREDENTIAL),
        mappings={"api_key": _INSTANCE_CREDENTIAL},
    )
    await _execute_to_the_credential_seam(svc)
    assert broker.calls[0]["credential_id"] == _INSTANCE_CREDENTIAL


async def test_the_fallback_resolve_stays_scoped_to_the_calling_org() -> None:
    """§3.3 / ADR-006: the fallback must not become a cross-tenant read of a stored credential id.
    The broker call carries the CALLER's organisation, so the id is resolved in that org only."""
    svc, broker = _svc(_mcp_descriptor(credential_id=_IMPORT_CREDENTIAL), mappings={})
    await _execute_to_the_credential_seam(svc, organisation_id=_OTHER_ORG)
    assert broker.calls[0]["organisation_id"] == _OTHER_ORG


async def test_an_unresolvable_fallback_still_fails_closed_with_needs_credential() -> None:
    """The fallback must never soften ADR-039: a declared requirement that resolves to nothing is
    a typed ``needs_credential``, never a silent anonymous call to the external server."""
    svc, _ = _svc(
        _mcp_descriptor(credential_id=_IMPORT_CREDENTIAL),
        mappings={},
        raise_exc=CredentialResolutionError("nope", error_code="credential_not_found"),
    )
    with pytest.raises(ExecutionNotReadyError) as err:
        await _execute(svc)
    assert err.value.detail is not None
    assert "needs_credential" in err.value.detail


async def test_a_public_server_import_declares_nothing_and_never_calls_the_broker() -> None:
    """An anonymous import has no requirement and no credential id — it must keep running today's
    anonymous path, not start fail-closing."""
    descriptor = _mcp_descriptor()
    descriptor["spec"].pop("credential_requirements")
    svc, broker = _svc(descriptor, mappings={})
    await _execute_to_the_credential_seam(svc)
    assert broker.calls == []


async def test_a_pending_tool_is_still_refused_before_any_credential_work() -> None:
    """The supply-chain gate is untouched by D2 — an unapproved tool never reaches the broker."""
    svc, broker = _svc(
        _mcp_descriptor(credential_id=_IMPORT_CREDENTIAL), status="pending_approval", mappings={}
    )
    with pytest.raises(ExecutionNotReadyError) as err:
        await _execute(svc)
    assert err.value.error_code == "pending_approval"
    assert broker.calls == []


async def test_the_stored_credential_id_never_appears_in_the_failure_detail() -> None:
    """#483 envelope discipline: the caller learns the requirement type and provider, not an id."""
    svc, _ = _svc(
        _mcp_descriptor(credential_id=_IMPORT_CREDENTIAL),
        mappings={},
        raise_exc=CredentialResolutionError("nope", error_code="credential_not_found"),
    )
    with pytest.raises(ExecutionNotReadyError) as err:
        await _execute(svc)
    assert _IMPORT_CREDENTIAL not in str(err.value.detail)
    assert _IMPORT_CREDENTIAL not in str(err.value)
