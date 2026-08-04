"""Unit: ToolExecutionService hands the manifest-validate gate the org's capability repository.

#705 — the gate's allowed set must come from the registry, by code, on every call. The connector is
a DOMAIN object and never touches the database itself; the SERVICES layer injects the repository at
execute time, exactly as it already does for ``GitHubSinkConnector.delivery_repo``. Without this
wiring the gate silently falls back to its built-ins-only floor on the live path and every imported
MCP tool blocks a compile again.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_capability_registry_service.domain.connectors.manifest_validate import (
    ManifestValidateConnector,
)
from oraclous_capability_registry_service.domain.plugins.builtin import ManifestValidatePlugin
from oraclous_capability_registry_service.schema.execution_schema import ExecuteRequest
from oraclous_capability_registry_service.services.tool_execution_service import (
    ToolExecutionService,
)

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_CAP = uuid.uuid4()
_INST = uuid.uuid4()
_USER = uuid.uuid4()
_EXEC = uuid.uuid4()


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


class _FakeCaps:
    """The capability repository — both the descriptor lookup AND the gate's catalog source."""

    async def get_by_id(self, capability_id: uuid.UUID, organisation_id: uuid.UUID) -> Any:  # noqa: ARG002
        return SimpleNamespace(
            organisation_id=_ORG, status="active", descriptor=ManifestValidatePlugin.descriptor()
        )

    async def list_by_org(self, organisation_id: uuid.UUID) -> list[Any]:  # noqa: ARG002
        return []

    async def list_by_kind(self, organisation_id: uuid.UUID, kind: Any) -> list[Any]:  # noqa: ARG002
        return []


class _FakeExecutions:
    async def create_queued(self, **_k: object) -> Any:
        return SimpleNamespace(id=_EXEC)

    async def finalize(self, **kwargs: Any) -> Any:
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
            credits_consumed=Decimal(0),
            processing_time_ms=0,
            created_at=None,
        )


async def test_the_execution_service_injects_the_capability_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = _FakeCaps()
    captured: list[ManifestValidateConnector] = []
    import oraclous_capability_registry_service.services.tool_execution_service as svc_mod

    real_create = svc_mod.create_executor

    def _spy(descriptor: dict[str, Any]) -> Any:
        executor = real_create(descriptor)
        if isinstance(executor, ManifestValidateConnector):
            captured.append(executor)
        return executor

    monkeypatch.setattr(svc_mod, "create_executor", _spy)
    svc = ToolExecutionService(
        instances=_FakeInstances(), capabilities=caps, executions=_FakeExecutions(), broker=None
    )
    await svc.execute_sync(
        instance_id=_INST,
        body=ExecuteRequest(input_data={"draft": {"members": []}}),
        organisation_id=_ORG,
        user_id=_USER,
    )
    assert captured, "the manifest-validate executor never ran"
    assert captured[0].capability_repo is caps
