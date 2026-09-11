"""Synchronous tool execution (services layer; reshape of legacy
``oraclous-core-service/app/services/tool_execution_service.py``).

The execution spine: validate readiness → resolve credentials via the broker seam → record a
QUEUED execution row (the service's own operational bookkeeping, not the §3.7 audit record —
see ``ExecutionRepository``) → dispatch the executor (hard timeout in the executor) → persist
the outcome with ``credential_refs`` (types/scopes used, never the secret) and scrub the
in-memory credentials → bump the instance counters. Async/queued execution is out of scope
(→ R5); this is sync only.

Every dispatch AND every pre-dispatch refusal emits one ``ProvenanceCollector`` record
(CLAUDE.md §3.7; the 24 August ruling): a successful/failed executor result emits
``capability.invoke``; each of the five readiness gates that raise before the operational row
is written emits ``capability.refused`` — before the exception propagates, and without ever
touching ``ExecutionRepository`` (24 August ruling §2).
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from oraclous_substrate import ProvenanceCollector, ProvenanceRecord, hash_payload

from oraclous_capability_registry_service.domain.connectors.github_sink import GitHubSinkConnector
from oraclous_capability_registry_service.domain.connectors.manifest_refine import (
    ManifestRefineConnector,
)
from oraclous_capability_registry_service.domain.connectors.manifest_validate import (
    ManifestValidateConnector,
)
from oraclous_capability_registry_service.domain.credentials import required_credentials
from oraclous_capability_registry_service.domain.errors import CapabilityNotFoundError
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import (
    NoExecutorError,
    create_executor,
    has_executor,
)
from oraclous_capability_registry_service.domain.operations import (
    OPERATION_KEY,
    UNSUPPORTED_OPERATION,
    operation_is_declared,
    unsupported_operation_message,
)
from oraclous_capability_registry_service.models.enums import ExecutionStatus, InstanceStatus
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.repositories.delivery_state_repository import (
    DeliveryStateRepository,
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


def _capped(value: object, *, limit: int = 64) -> object:
    """Bound a user-authored requirement field so the leak-safe ``needs_credential`` token can never
    become a reflected-value relay channel once it is surfaced. ``type`` is enum-validated at
    descriptor ingest, but a non-oauth requirement's ``provider`` is otherwise uncapped — capping
    it here makes the token safe BY CONSTRUCTION (the gateway strips it today; the #502 FE Contract
    will relay it), independent of any downstream sanitiser (#483 envelope discipline)."""
    return value[:limit] if isinstance(value, str) else value


async def emit_dispatch_provenance(
    provenance: ProvenanceCollector,
    *,
    organisation_id: uuid.UUID,
    principal_id: uuid.UUID,
    resource: str,
    outcome: str,
    input_hash: str | None = None,
    output_hash: str | None = None,
) -> None:
    """``provenance-on-dispatch`` seam (``tools/lint/seam_wiring.yaml``, #826 solution-architect
    ruling item 5): a capability dispatch on a service request path must produce a provenance
    record through the runtime's single collector (CLAUDE.md §3.7), never a direct database write.
    Wired here on the registry's own ``execute_sync`` dispatch (below); the execution-engine's
    adopted-tool dispatch (``tasks/run_tasks.py::_run_adopted_tool_async``) wires the same seam on
    its own dispatch path."""
    await provenance.emit(
        ProvenanceRecord(
            organisation_id=str(organisation_id),
            principal=str(principal_id),
            action="capability.invoke",
            resource=resource,
            outcome=outcome,
            input_hash=input_hash,
            output_hash=output_hash,
        )
    )


class ToolExecutionService:
    def __init__(
        self,
        *,
        instances: InstanceRepository,
        capabilities: CapabilityRepository,
        executions: ExecutionRepository,
        broker: CredentialBrokerPort,
        provenance: ProvenanceCollector,
        delivery_state: DeliveryStateRepository | None = None,
    ) -> None:
        self._instances = instances
        self._capabilities = capabilities
        self._executions = executions
        self._broker = broker
        self._provenance = provenance
        # the deliver-back clean-delta store, injected into a GitHubSinkConnector at execute time
        # (None on a unit/test construction → the sink first-delivers everything, no persistence).
        self._delivery_state = delivery_state

    async def _emit_refused(
        self, *, instance_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID, code: str
    ) -> None:
        """A pre-dispatch readiness gate refused the call — record it BEFORE the caller's exception
        propagates. Never touches ``ExecutionRepository`` (24 August ruling §2: a refusal is not
        operational execution state)."""
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(organisation_id),
                principal=str(user_id),
                action="capability.refused",
                resource=f"tool_instance:{instance_id}",
                outcome=code,
                context={"error_code": code},
            )
        )

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
            await self._emit_refused(
                instance_id=instance_id,
                organisation_id=organisation_id,
                user_id=user_id,
                code="instance_not_found",
            )
            raise InstanceNotFoundError("instance not found")
        descriptor_row = await self._capabilities.get_by_id(instance.capability_id, organisation_id)
        if descriptor_row is None:
            raise CapabilityNotFoundError("capability not found")
        descriptor = descriptor_row.descriptor

        # supply-chain HITL gate (R6 MCP-import): an imported external MCP tool is not executable
        # until an org admin has approved it (status pending_approval -> active). Fail-closed.
        spec = descriptor.get("spec") or {}
        if spec.get("type") == "mcp" and descriptor_row.status != "active":
            await self._emit_refused(
                instance_id=instance_id,
                organisation_id=organisation_id,
                user_id=user_id,
                code="pending_approval",
            )
            raise ExecutionNotReadyError(
                "this imported MCP tool is pending admin approval",
                error_code="pending_approval",
            )

        # #1004 item 1 — defence in depth for #956. The harness runtime binds the operation at
        # dispatch, but the registry used to run whatever `operation` arrived and let each
        # connector's own hardcoded whitelist be the only check. Now the operation a caller CHOSE
        # must be one the INSTANCE's descriptor declares; the two services agree independently, and
        # a caller that chose nothing still gets the connector's default (see `domain.operations`).
        # Placed before the executor lookup: what the descriptor declares is the authority, whether
        # or not this deployment happens to ship an executor for the tool.
        if not operation_is_declared(descriptor, body.input_data):
            await self._emit_refused(
                instance_id=instance_id,
                organisation_id=organisation_id,
                user_id=user_id,
                code=UNSUPPORTED_OPERATION,
            )
            raise ExecutionNotReadyError(
                unsupported_operation_message(body.input_data.get(OPERATION_KEY)),
                error_code=UNSUPPORTED_OPERATION,
            )

        if not has_executor(descriptor):
            await self._emit_refused(
                instance_id=instance_id,
                organisation_id=organisation_id,
                user_id=user_id,
                code="no_executor",
            )
            raise ExecutionNotReadyError(
                "no executor is available for this tool",
                error_code="no_executor",
            )

        # Resolve every required credential via the broker seam (fail-closed; no execution on miss).
        requirements = required_credentials(descriptor)
        mappings = dict(instance.credential_mappings or {})
        # #698 D2: an MCP import records the key the org admin deliberately chose for that server
        # (spec.credential_id, #541). A member's run never maps a credential onto the instance, so
        # without this fallback the resolve went out with no id and the tools/call was anonymous —
        # a 401 from any hosted server. An instance mapping still wins: this is a fallback only,
        # and it is resolved under the CALLING org, so it can never reach another tenant's key.
        import_credential_id = (descriptor.get("spec") or {}).get("credential_id")
        credentials: dict[str, Any] = {}
        credential_refs: list[dict[str, Any]] = []
        for req in requirements:
            try:
                resolved = await self._broker.resolve(
                    organisation_id=organisation_id,
                    user_id=user_id,
                    requirement=req,
                    # req["type"] is a str when present; a None miss is tolerated by the lookup.
                    credential_id=mappings.get(cast("str", req.get("type")))
                    or import_credential_id,
                )
            except CredentialResolutionError as exc:
                # O1 "no auth-prompt wall" (ADR-039): a satisfied requirement dispatches
                # silently; a missing one fails closed with a typed, leak-safe needs_credential
                # token so the caller knows EXACTLY which credential to onboard — requirement_id
                # + provider ONLY, NEVER a value or credential_id (#483 envelope discipline).
                # The store (POST /credentials/) + resolve path are already built; this completes
                # the signal on the miss so the user can paste the key once and re-run.
                await self._emit_refused(
                    instance_id=instance_id,
                    organisation_id=organisation_id,
                    user_id=user_id,
                    code=exc.error_code,
                )
                raise ExecutionNotReadyError(
                    str(exc),
                    error_code=exc.error_code,
                    detail={
                        "needs_credential": {
                            "requirement_id": req.get("type"),
                            "provider": _capped(req.get("provider")),
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

        instance_config = dict(instance.configuration or {})
        # File-native blackboard (ADR-040 / #512): a declared working tree (the team's git-markdown
        # tree, or a team run's workspace_root the harness writes into each file-tool instance's
        # config) makes the file tools operate IN PLACE on it. None → the default per-org scratch.
        working_dir = instance_config.get("working_dir")
        context = ExecutionContext(
            instance_id=instance_id,
            organisation_id=organisation_id,
            user_id=user_id,
            execution_id=execution.id,
            principal_type=principal_type,
            credentials=credentials,
            configuration=instance_config,
            settings=dict(instance.settings or {}),
            working_dir=str(working_dir) if working_dir else None,
        )
        try:
            executor = create_executor(descriptor)
            if isinstance(executor, GitHubSinkConnector):
                # the sink owns the clean-delta; the service owns the DB repo (keeps the executor a
                # pure descriptor object, mirroring how executions/instances are service-held).
                executor.delivery_repo = self._delivery_state
            if isinstance(executor, ManifestValidateConnector):
                # #705: the compile gate's allowed set is READ from the org's registry, never
                # relayed by the reviewer LLM. Same shape as the sink above — the connector stays a
                # domain object and the services layer owns the DB repo.
                executor.capability_repo = self._capabilities
            if isinstance(executor, ManifestRefineConnector):
                # #708: the identical fix for the refine gate — same reasoning as #705 above.
                executor.capability_repo = self._capabilities
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
        await emit_dispatch_provenance(
            self._provenance,
            organisation_id=organisation_id,
            principal_id=user_id,
            resource=f"tool_instance:{instance_id}",
            outcome="succeeded" if result.success else "failed",
            input_hash=hash_payload(body.input_data),
            output_hash=hash_payload(output),
        )
        return ExecutionOut.model_validate(finalized)
