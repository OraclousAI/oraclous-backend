"""Unit: ToolExecutionService hands the new ``draft-manifest`` connector the org's capability
repository — the SAME wiring #705 gave ``ManifestValidateConnector`` (mirrors
``test_manifest_validate_repo_injection.py`` exactly; see that file's own docstring for the full
reasoning, unchanged here).

``draft-manifest`` is a not-yet-built #900 seam (``DraftManifestConnector``/``DraftManifestPlugin``
exist nowhere on ``main``): the import is function-local, inside the one test below, per
``.claude/rules/tests-seam-imports.md`` — a module-level import would abort collection for this
entire test run. Without this wiring the gate silently falls back to its built-ins-only floor on
the live path and an imported MCP tool blocks a compile again, exactly as #705 originally found for
``manifest-validate``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
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

    def __init__(self, descriptor: dict[str, Any]) -> None:
        self._descriptor = descriptor

    async def get_by_id(self, capability_id: uuid.UUID, organisation_id: uuid.UUID) -> Any:  # noqa: ARG002
        return SimpleNamespace(organisation_id=_ORG, status="active", descriptor=self._descriptor)

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


class _NoopProvenance:
    """Not under test here (#826 owns provenance emit coverage) — a discard sink so the
    now-mandatory constructor kwarg does not force this file to assert on it."""

    async def emit(self, record: Any) -> None:  # noqa: ANN401
        return None


async def test_the_execution_service_injects_the_capability_repository_into_draft_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oraclous_capability_registry_service.domain.connectors.draft_manifest import (
        DraftManifestConnector,
    )
    from oraclous_capability_registry_service.domain.plugins.builtin import DraftManifestPlugin

    caps = _FakeCaps(DraftManifestPlugin.descriptor())
    captured: list[DraftManifestConnector] = []
    import oraclous_capability_registry_service.services.tool_execution_service as svc_mod

    real_create = svc_mod.create_executor

    def _spy(descriptor: dict[str, Any]) -> Any:
        executor = real_create(descriptor)
        if isinstance(executor, DraftManifestConnector):
            captured.append(executor)
        return executor

    monkeypatch.setattr(svc_mod, "create_executor", _spy)
    svc = ToolExecutionService(
        instances=_FakeInstances(),
        capabilities=caps,
        executions=_FakeExecutions(),
        broker=None,
        provenance=_NoopProvenance(),  # type: ignore[arg-type]
    )
    # input_data IS the drafted team directly (no "draft" wrapper key — #900 design decision; see
    # test_draft_manifest_connector.py's module docstring for the full justification).
    await svc.execute_sync(
        instance_id=_INST,
        body=ExecuteRequest(input_data={"members": []}),
        organisation_id=_ORG,
        user_id=_USER,
    )
    assert captured, "the draft-manifest executor never ran"
    assert captured[0].capability_repo is caps
