"""Harness execution spine (ORAA-4 §21 services layer).

The use-case that turns an OHM + an input into a completed run: load the manifest → resolve the
entrypoint capability against the registry → materialise an instance (+ configure its credential
mappings from the OHM capability ``config``) → build the tool schemas → run the tool-use loop, where
each tool call is dispatched to the registry's real execute → emit one provenance event per step +
a closure event → persist the harness execution row. OHM errors propagate to the route (client 422);
a registry *setup* failure becomes a ``HarnessExecutionError`` (502); per-tool failures are fed back
into the loop (the agent observes and adapts), never aborting the run.
"""

from __future__ import annotations

import uuid
from typing import Any

from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.llm.factory import build_llm_client
from oraclous_harness_runtime_service.domain.loop.tool_use import (
    LoopResult,
    run_tool_use_loop,
)
from oraclous_harness_runtime_service.domain.ohm.parse import load_ohm
from oraclous_harness_runtime_service.domain.tool_schemas import tool_specs_for
from oraclous_harness_runtime_service.models.enums import StepKind
from oraclous_harness_runtime_service.models.execution import HarnessExecution
from oraclous_harness_runtime_service.repositories.execution_repository import ExecutionRepository
from oraclous_harness_runtime_service.services.registry_client import RegistryClient, RegistryError


class HarnessExecutionError(Exception):
    """A harness could not be set up to run (a dependency/registry failure). Maps to HTTP 502."""


class HarnessExecutionService:
    def __init__(
        self,
        *,
        registry: RegistryClient,
        executions: ExecutionRepository,
        provenance: ProvenanceCollector,
        llm_mode: str,
        max_iterations: int,
    ) -> None:
        self._registry = registry
        self._executions = executions
        self._provenance = provenance
        self._llm_mode = llm_mode
        self._max_iterations = max_iterations

    async def execute(
        self, *, manifest_raw: str | dict[str, Any], user_input: str, principal: Principal
    ) -> HarnessExecution:
        manifest = load_ohm(manifest_raw)  # OHMError → route maps to 422
        # Fail-closed tenancy (ADR-006/T1-M1): the org comes from the authenticated principal ONLY —
        # never from the client-controlled manifest. The route guarantees it in every auth mode.
        if principal.organisation_id is None:
            raise HarnessExecutionError("authenticated principal has no organisation scope")
        org_id = principal.organisation_id
        execution_id = uuid.uuid4()
        resource = f"harness_execution:{execution_id}"
        prov_principal = str(principal.principal_id)

        # Resolve the entrypoint capability → a registry instance (+ credentials).
        entry = manifest.entrypoint_capability()
        if entry is None:  # defensive — load_ohm already cross-checks this
            raise HarnessExecutionError("OHM runtime.entrypoint does not resolve to a capability")
        try:
            cap_item = await self._registry.resolve_capability(
                entry.ref, explicit_id=entry.config.get("capability_id")
            )
            instance = await self._registry.create_instance(
                capability_id=str(cap_item["id"]),
                name=f"harness:{manifest.metadata.id}:{entry.binding}",
                configuration={
                    k: v
                    for k, v in entry.config.items()
                    if k not in ("credential_mappings", "capability_id")
                },
            )
            instance_id = uuid.UUID(str(instance["id"]))
            mappings = entry.config.get("credential_mappings") or {}
            if mappings:
                await self._registry.configure_credentials(instance_id, mappings)
        except RegistryError as exc:
            raise HarnessExecutionError(f"capability setup failed: {exc}") from exc

        tool_specs = tool_specs_for(entry.binding, cap_item.get("descriptor") or {})

        async def dispatch(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
            execution = await self._registry.execute(
                instance_id, {"operation": spec.operation, **args}
            )
            if execution.get("status") != "SUCCESS":
                detail = execution.get("error_message") or execution.get("status")
                raise RegistryError(f"tool execution failed: {detail}")
            return execution.get("output_data") or {}

        prompt = manifest.primary_prompt()
        llm = build_llm_client(self._llm_mode)
        result = await run_tool_use_loop(
            llm=llm,
            system=prompt.body if prompt else "",
            user_input=user_input,
            tool_specs=tool_specs,
            dispatch=dispatch,
            max_iterations=self._max_iterations,
        )

        # Persist the durable run record FIRST, then emit provenance — an audit-emit failure must
        # never discard a run whose side effects (real registry executions) have already happened.
        row = await self._executions.create(
            execution_id=execution_id,
            organisation_id=org_id,
            user_id=principal.principal_id,
            harness_id=manifest.metadata.id,
            harness_name=manifest.metadata.name,
            status=result.status.value,
            input_text=user_input,
            output=result.output,
            error_type=result.error_type,
            error_message=result.error_message,
            iterations=result.iterations,
            steps=[
                {
                    "index": s.index,
                    "kind": s.kind.value,
                    "name": s.name,
                    "status": s.status,
                    "detail": s.detail,
                }
                for s in result.steps
            ],
        )
        await self._emit_provenance(
            result, org_id=str(org_id), principal=prov_principal, resource=resource
        )
        return row

    async def _emit_provenance(
        self, result: LoopResult, *, org_id: str, principal: str, resource: str
    ) -> None:
        """One provenance event per step + a closure event (the single write-through path)."""
        for step in result.steps:
            action = "llm.complete" if step.kind == StepKind.LLM else "capability.invoke"
            # coalesce so a model-supplied (possibly empty) tool name can't fail the required-field
            # contract on the substrate collector.
            outcome = f"{step.name or '<unnamed>'}:{step.status or 'unknown'}"
            await self._provenance.emit(
                ProvenanceRecord(
                    organisation_id=org_id,
                    principal=principal,
                    action=action,
                    resource=resource,
                    outcome=outcome,
                )
            )
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=org_id,
                principal=principal,
                action="harness.execute",
                resource=resource,
                outcome=result.status.value,
            )
        )
