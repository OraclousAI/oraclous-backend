"""Harness execution spine (ORAA-4 §21 services layer).

Turns an OHM + an input into a completed run:
  source the manifest (inline YAML/object, or a ``manifest_ref`` → a registered kind=harness
  descriptor) → verify signatures against the trust store → compute the content hash → validate the
  schema → **atomically resolve every capability** against the registry (all-or-nothing) →
  materialise an instance per capability (+ its credential mappings) → expose the union of all
  capabilities as the agent's tools → run the tool-use loop, dispatching each call to the registry's
  real execute → persist the run row → emit a provenance event per step + a closure event.

OHM errors (parse/schema/version/reference/signature) propagate to the route (422); a registry
*setup* failure is a ``HarnessExecutionError`` (502); per-tool failures are fed back into the loop.
"""

from __future__ import annotations

import uuid
from typing import Any

import yaml
from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.llm.factory import build_llm_client
from oraclous_harness_runtime_service.domain.loop.tool_use import LoopResult, run_tool_use_loop
from oraclous_harness_runtime_service.domain.ohm.canonical import content_hash
from oraclous_harness_runtime_service.domain.ohm.errors import OHMParseError, OHMReferenceError
from oraclous_harness_runtime_service.domain.ohm.parse import load_ohm
from oraclous_harness_runtime_service.domain.ohm.references import resolve_capabilities
from oraclous_harness_runtime_service.domain.ohm.signatures import TrustStore, verify_signatures
from oraclous_harness_runtime_service.domain.tool_schemas import tool_specs_for
from oraclous_harness_runtime_service.models.enums import StepKind
from oraclous_harness_runtime_service.models.execution import HarnessExecution
from oraclous_harness_runtime_service.repositories.execution_repository import ExecutionRepository
from oraclous_harness_runtime_service.services.registry_client import RegistryClient, RegistryError

_RESERVED_CONFIG_KEYS = ("credential_mappings", "capability_id")


class HarnessExecutionError(Exception):
    """A harness could not be set up to run (a dependency/registry failure). Maps to HTTP 502."""


class HarnessExecutionService:
    def __init__(
        self,
        *,
        registry: RegistryClient,
        executions: ExecutionRepository,
        provenance: ProvenanceCollector,
        trust: TrustStore,
        require_signature: bool,
        llm_mode: str,
        max_iterations: int,
    ) -> None:
        self._registry = registry
        self._executions = executions
        self._provenance = provenance
        self._trust = trust
        self._require_signature = require_signature
        self._llm_mode = llm_mode
        self._max_iterations = max_iterations

    async def execute(
        self,
        *,
        manifest_inline: str | dict[str, Any] | None,
        manifest_ref: str | None,
        user_input: str,
        principal: Principal,
    ) -> HarnessExecution:
        # Fail-closed tenancy (ADR-006/T1-M1): org is the principal's ONLY, never the manifest's.
        if principal.organisation_id is None:
            raise HarnessExecutionError("authenticated principal has no organisation scope")
        org_id = principal.organisation_id

        # Source + harden the manifest (load-time, atomic; OHMError → 422).
        document = await self._source_document(manifest_inline, manifest_ref)
        verify_signatures(document, self._trust, require=self._require_signature)
        chash = content_hash(document)
        manifest = load_ohm(document)
        resolved = await self._resolve_all(manifest)

        execution_id = uuid.uuid4()
        resource = f"harness_execution:{execution_id}"

        # Materialise an instance per capability + expose the union as the agent's tools.
        instance_by_binding, tool_specs = await self._materialise(manifest, resolved)

        async def dispatch(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
            instance_id = instance_by_binding.get(spec.binding)
            if instance_id is None:  # invariant: every emitted tool_spec.binding was materialised
                raise RegistryError(f"no instance for capability binding {spec.binding!r}")
            execution = await self._registry.execute(
                instance_id, {"operation": spec.operation, **args}
            )
            if execution.get("status") != "SUCCESS":
                detail = execution.get("error_message") or execution.get("status")
                raise RegistryError(f"tool execution failed: {detail}")
            return execution.get("output_data") or {}

        prompt = manifest.primary_prompt()
        result = await run_tool_use_loop(
            llm=build_llm_client(self._llm_mode),
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
            content_hash=chash,
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
            result, org_id=str(org_id), principal=str(principal.principal_id), resource=resource
        )
        return row

    async def _source_document(
        self, manifest_inline: str | dict[str, Any] | None, manifest_ref: str | None
    ) -> dict[str, Any]:
        """Produce the OHM document from inline YAML/object or a registered harness reference."""
        if manifest_ref is not None:
            try:
                item = await self._registry.get_capability(manifest_ref)
            except RegistryError as exc:
                raise OHMReferenceError(
                    f"manifest_ref {manifest_ref!r} not resolvable: {exc}"
                ) from exc
            if item.get("kind") != "harness":
                raise OHMReferenceError(
                    f"manifest_ref {manifest_ref!r} is a {item.get('kind')!r}, not a harness"
                )
            document = item.get("descriptor")
        elif isinstance(manifest_inline, str):
            try:
                document = yaml.safe_load(manifest_inline)
            except yaml.YAMLError as exc:
                raise OHMParseError(f"OHM YAML is malformed: {exc}") from exc
        else:
            document = manifest_inline
        if not isinstance(document, dict):
            raise OHMParseError("OHM document must be a mapping at the top level")
        return document

    async def _resolve_all(self, manifest) -> dict[str, dict[str, Any]]:  # noqa: ANN001
        async def resolve(ref: str, explicit_id: str | None) -> dict[str, Any]:
            return await self._registry.resolve_capability(ref, explicit_id=explicit_id)

        return await resolve_capabilities(manifest, resolve)  # OHMReferenceError → 422

    async def _materialise(
        self,
        manifest,
        resolved: dict[str, dict[str, Any]],  # noqa: ANN001
    ) -> tuple[dict[str, uuid.UUID], list[ToolSpec]]:
        """Find-or-create a registry instance per capability + build the agent's full toolset.

        Idempotent: each capability maps to a deterministically-named instance
        (``harness:<id>:<binding>``), reused across runs rather than recreated — so retries don't
        accumulate instances and a partial setup failure has a bounded, reusable footprint (the
        registry has no instance-delete endpoint to compensate-delete against). Tool names are
        ``<binding>__<operation>``; bindings are load-time-unique (parse), so they never collide.
        """
        instance_by_binding: dict[str, uuid.UUID] = {}
        tool_specs: list[ToolSpec] = []
        seen_tools: set[str] = set()
        try:
            existing = {i.get("name"): i for i in await self._registry.list_instances()}
            for cap in manifest.capabilities:
                item = resolved[cap.binding]
                name = f"harness:{manifest.metadata.id}:{cap.binding}"
                prior = existing.get(name)
                if prior is not None and str(prior.get("capability_id")) == str(item["id"]):
                    instance_id = uuid.UUID(str(prior["id"]))
                else:
                    instance = await self._registry.create_instance(
                        capability_id=str(item["id"]),
                        name=name,
                        configuration={
                            k: v for k, v in cap.config.items() if k not in _RESERVED_CONFIG_KEYS
                        },
                    )
                    instance_id = uuid.UUID(str(instance["id"]))
                instance_by_binding[cap.binding] = instance_id
                mappings = cap.config.get("credential_mappings") or {}
                if mappings:
                    await self._registry.configure_credentials(instance_id, mappings)
                for spec in tool_specs_for(cap.binding, item.get("descriptor") or {}):
                    if (
                        spec.name in seen_tools
                    ):  # de-dup duplicate operation names within a descriptor
                        continue
                    seen_tools.add(spec.name)
                    tool_specs.append(spec)
        except RegistryError as exc:
            raise HarnessExecutionError(f"capability setup failed: {exc}") from exc
        return instance_by_binding, tool_specs

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
