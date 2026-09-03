"""Unit: ToolExecutionService hands the manifest-refine gate the org's capability repository.

#708 — the same wiring #705 already gave ``manifest-validate``: the connector is a DOMAIN object
and never touches the database itself; the SERVICES layer injects the repository at execute time.
Without this wiring the refine gate silently falls back to its built-ins-only floor on the live
path and every imported MCP tool blocks a refine again — mirrors
``test_manifest_validate_repo_injection.py`` exactly, swapping the plugin/connector.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_capability_registry_service.domain.connectors.manifest_refine import (
    ManifestRefineConnector,
)
from oraclous_capability_registry_service.domain.plugins.builtin import ManifestRefinePlugin
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
            organisation_id=_ORG, status="active", descriptor=ManifestRefinePlugin.descriptor()
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
    captured: list[ManifestRefineConnector] = []
    import oraclous_capability_registry_service.services.tool_execution_service as svc_mod

    real_create = svc_mod.create_executor

    def _spy(descriptor: dict[str, Any]) -> Any:
        executor = real_create(descriptor)
        if isinstance(executor, ManifestRefineConnector):
            captured.append(executor)
        return executor

    monkeypatch.setattr(svc_mod, "create_executor", _spy)
    svc = ToolExecutionService(
        instances=_FakeInstances(), capabilities=caps, executions=_FakeExecutions(), broker=None
    )
    await svc.execute_sync(
        instance_id=_INST,
        body=ExecuteRequest(
            input_data={
                "manifest": {
                    "ohm_version": "1.1",
                    "metadata": {
                        "id": str(uuid.uuid4()),
                        "name": "t",
                        "owner_organization_id": str(_ORG),
                        "kind": "team",
                    },
                    "members": [
                        {"role": "a", "kind": "agent", "manifest_ref": "org:x/a@1"},
                    ],
                    "runtime": {"entrypoint": "a"},
                },
                "edit_op": {"op": "add_depends_on", "role": "a", "depends_on": "a"},
            }
        ),
        organisation_id=_ORG,
        user_id=_USER,
    )
    assert captured, "the manifest-refine executor never ran"
    assert captured[0].capability_repo is caps
