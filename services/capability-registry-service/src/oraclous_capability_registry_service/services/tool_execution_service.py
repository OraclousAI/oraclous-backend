"""Synchronous tool execution (ORAA-4 §21 services layer; reshape of legacy
``oraclous-core-service/app/services/tool_execution_service.py``).

The execution spine: validate readiness → resolve credentials via the broker seam → record a QUEUED
provenance row → dispatch the executor (hard timeout in the executor) → persist the outcome with
``credential_refs`` (types/scopes used, never the secret) and scrub the in-memory credentials → bump
the instance counters. Async/queued execution is out of scope (→ R5); this is sync only.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from oraclous_capability_registry_service.domain.errors import CapabilityNotFoundError
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import (
    NoExecutorError,
    create_executor,
    has_executor,
)
from oraclous_capability_registry_service.models.enums import ExecutionStatus, InstanceStatus
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.repositories.execution_repository import (
    ExecutionRepository,
)
from oraclous_capability_registry_service.repositories.instance_repository import InstanceRepository
from oraclous_capability_registry_service.schema.execution_schema import (
    ExecuteRequest,
    ExecutionOut,
)
from oraclous_capability_registry_service.services.credential_client import (
    CredentialBrokerPort,
    CredentialResolutionError,
)
from oraclous_capability_registry_service.services.instance_manager import InstanceNotFoundError


class ExecutionNotReadyError(Exception):
    """The instance cannot run (missing/unresolved credential or no executor). Maps to HTTP 409."""

    def __init__(self, message: str, *, error_code: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.detail = detail or {}


def _credential_requirements(descriptor: dict[str, Any]) -> list[dict[str, Any]]:
    spec = descriptor.get("spec") or {}
    return [
        r
        for r in (spec.get("credential_requirements") or [])
        if isinstance(r, dict) and r.get("required", True)
    ]


class ToolExecutionService:
    def __init__(
        self,
        *,
        instances: InstanceRepository,
        capabilities: CapabilityRepository,
        executions: ExecutionRepository,
        broker: CredentialBrokerPort,
    ) -> None:
        self._instances = instances
        self._capabilities = capabilities
        self._executions = executions
        self._broker = broker

    async def execute_sync(
        self,
        *,
        instance_id: uuid.UUID,
        body: ExecuteRequest,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        principal_type: str = "agent",
    ) -> ExecutionOut:
        instance = await self._instances.get_by_id(instance_id, organisation_id)
        if instance is None:
            raise InstanceNotFoundError("instance not found")
        descriptor_row = await self._capabilities.get_by_id(instance.capability_id, organisation_id)
        if descriptor_row is None:
            raise CapabilityNotFoundError("capability not found")
        descriptor = descriptor_row.descriptor

        # supply-chain HITL gate (R6 MCP-import): an imported external MCP tool is not executable
        # until an org admin has approved it (status pending_approval -> active). Fail-closed.
        spec = descriptor.get("spec") or {}
        if spec.get("type") == "mcp" and descriptor_row.status != "active":
            raise ExecutionNotReadyError(
                "this imported MCP tool is pending admin approval",
                error_code="pending_approval",
            )

        if not has_executor(descriptor):
            raise ExecutionNotReadyError(
                "no executor is available for this tool",
                error_code="no_executor",
            )

        # Resolve every required credential via the broker seam (fail-closed; no execution on miss).
        requirements = _credential_requirements(descriptor)
        mappings = dict(instance.credential_mappings or {})
        credentials: dict[str, Any] = {}
        credential_refs: list[dict[str, Any]] = []
        for req in requirements:
            try:
                resolved = await self._broker.resolve(
                    organisation_id=organisation_id,
                    user_id=user_id,
                    requirement=req,
                    # req["type"] is a str when present; a None miss is tolerated by the lookup.
                    credential_id=mappings.get(cast("str", req.get("type"))),
                )
            except CredentialResolutionError as exc:
                # O1 "no auth-prompt wall" (ADR-039): a satisfied requirement dispatches
                # silently; a missing one fails closed with a typed, leak-safe needs_credential
                # token so the caller knows EXACTLY which credential to onboard — requirement_id
                # + provider ONLY, NEVER a value or credential_id (#483 envelope discipline).
                # The store (POST /credentials/) + resolve path are already built; this completes
                # the signal on the miss so the user can paste the key once and re-run.
                raise ExecutionNotReadyError(
                    str(exc),
                    error_code=exc.error_code,
                    detail={
                        "needs_credential": {
                            "requirement_id": req.get("type"),
                            "provider": req.get("provider"),
                        },
                        "login_url": exc.login_url,
                        "missing_scopes": exc.missing_scopes,
                    },
                ) from exc
            credentials[resolved.credential_type] = resolved.payload
            credential_refs.append(
                {
                    "type": req.get("type"),
                    "provider": req.get("provider"),
                    "scopes": req.get("scopes", []),
                }
            )

        execution = await self._executions.create_queued(
            organisation_id=organisation_id,
            instance_id=instance_id,
            capability_id=instance.capability_id,
            user_id=user_id,
            input_data=body.input_data,
            credential_refs=credential_refs,
        )

        context = ExecutionContext(
            instance_id=instance_id,
            organisation_id=organisation_id,
            user_id=user_id,
            execution_id=execution.id,
            principal_type=principal_type,
            credentials=credentials,
            configuration=dict(instance.configuration or {}),
            settings=dict(instance.settings or {}),
        )
        try:
            executor = create_executor(descriptor)
            result = await executor.execute(body.input_data, context)
        except NoExecutorError as exc:  # defensive — has_executor already gated this
            raise ExecutionNotReadyError(str(exc), error_code="no_executor") from exc
        finally:
            context.scrub()

        status = ExecutionStatus.SUCCESS if result.success else ExecutionStatus.FAILED
        output = result.data if isinstance(result.data, dict) else {"result": result.data}
        finalized = await self._executions.finalize(
            execution_id=execution.id,
            organisation_id=organisation_id,
            status=status,
            output_data=output if result.success else None,
            error_message=result.error_message,
            error_type=result.error_type,
            credits_consumed=result.credits_consumed,
            processing_time_ms=result.processing_time_ms,
        )
        await self._instances.record_execution(
            instance_id,
            organisation_id,
            execution_id=execution.id,
            status=InstanceStatus.SUCCESS if result.success else InstanceStatus.FAILED,
            credits_consumed=result.credits_consumed,
        )
        assert finalized is not None  # noqa: S101 — just created in this txn
        return ExecutionOut.model_validate(finalized)
