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

from oraclous_harness_runtime_service.domain.llm.base import LLMClient, ToolSpec
from oraclous_harness_runtime_service.domain.llm.factory import (
    LLMConfigError,
    build_fake_client,
    build_live_client,
)
from oraclous_harness_runtime_service.domain.loop.tool_use import (
    LoopCheckpoint,
    LoopResult,
    LoopStep,
    run_tool_use_loop,
)
from oraclous_harness_runtime_service.domain.ohm.canonical import content_hash
from oraclous_harness_runtime_service.domain.ohm.errors import OHMParseError, OHMReferenceError
from oraclous_harness_runtime_service.domain.ohm.parse import load_ohm
from oraclous_harness_runtime_service.domain.ohm.references import resolve_capabilities
from oraclous_harness_runtime_service.domain.ohm.signatures import TrustStore, verify_signatures
from oraclous_harness_runtime_service.domain.policy import (
    build_envelope,
    enforce_load_policy,
    resolve_policy_set,
)
from oraclous_harness_runtime_service.domain.tool_schemas import tool_specs_for
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind
from oraclous_harness_runtime_service.models.execution import HarnessExecution
from oraclous_harness_runtime_service.repositories.assignment_repository import AssignmentRepository
from oraclous_harness_runtime_service.repositories.checkpoint_repository import CheckpointRepository
from oraclous_harness_runtime_service.repositories.execution_repository import ExecutionRepository
from oraclous_harness_runtime_service.services.broker_client import BrokerClient, BrokerError
from oraclous_harness_runtime_service.services.registry_client import RegistryClient, RegistryError

_RESERVED_CONFIG_KEYS = ("credential_mappings", "capability_id")


class HarnessExecutionError(Exception):
    """A harness could not be set up to run (a dependency/registry failure). Maps to HTTP 502."""


class ResumeError(Exception):
    """A mid-loop HITL run could not be resumed (missing/wrong-state). Carries the HTTP status."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _serialize_steps(steps: list[LoopStep], base: int = 0) -> list[dict[str, Any]]:
    """LoopSteps → the JSONB step-trace shape, re-indexed from ``base`` (resume appends a tail)."""
    return [
        {
            "index": base + i,
            "kind": s.kind.value,
            "name": s.name,
            "status": s.status,
            "detail": s.detail,
        }
        for i, s in enumerate(steps)
    ]


def _is_hitl_pause(result: LoopResult) -> bool:
    return (
        result.status is HarnessStatus.ESCALATED
        and result.error_type == "hitl_required"
        and result.checkpoint is not None
    )


def _cursor(checkpoint: LoopCheckpoint) -> dict[str, int]:
    return {
        "iteration": checkpoint.iteration,
        "tool_calls_made": checkpoint.tool_calls_made,
        "tokens_used": checkpoint.tokens_used,
    }


class HarnessExecutionService:
    def __init__(
        self,
        *,
        registry: RegistryClient,
        broker: BrokerClient,
        executions: ExecutionRepository,
        assignments: AssignmentRepository,
        checkpoints: CheckpointRepository,
        provenance: ProvenanceCollector,
        trust: TrustStore,
        require_signature: bool,
        force_policy_set: str | None,
        llm_mode: str,
        llm_base_urls: dict[str, str],
        llm_timeout: float,
        max_iterations: int,
    ) -> None:
        self._registry = registry
        self._broker = broker
        self._executions = executions
        self._assignments = assignments
        self._checkpoints = checkpoints
        self._provenance = provenance
        self._trust = trust
        self._require_signature = require_signature
        self._force_policy_set = force_policy_set
        self._llm_mode = llm_mode
        self._llm_base_urls = llm_base_urls
        self._llm_timeout = llm_timeout
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

        # Source + harden the manifest (load-time, atomic; OHMError → 422). The policy set (from
        # governance.policy_set_ref) drives coded enforcement: signature requirement, capability
        # allocation + BYOM limits at load, and the runtime budget/HITL/redaction envelope.
        document = await self._source_document(manifest_inline, manifest_ref)
        manifest = load_ohm(document)
        # A deployment-forced policy set overrides the author's choice (governance floor, M1).
        policy = resolve_policy_set(self._force_policy_set or manifest.governance.policy_set_ref)
        verify_signatures(
            document, self._trust, require=self._require_signature or policy.require_signature
        )
        enforce_load_policy(manifest, policy)
        chash = content_hash(document)

        # Actor dispatch: a human entrypoint actor halts the run as a task-board assignment
        # (R4 escalation; durable resume is R5). An agent actor (or no actors) runs the loop below.
        actor = manifest.entrypoint_actor()
        if actor is not None and actor.kind == "human":
            return await self._dispatch_human(manifest, actor, org_id, principal, user_input, chash)

        envelope, tool_specs, dispatch, llm = await self._build_runnable(manifest, policy, org_id)
        execution_id = uuid.uuid4()
        resource = f"harness_execution:{execution_id}"
        prompt = manifest.primary_prompt()
        try:
            result = await run_tool_use_loop(
                llm=llm,
                system=prompt.body if prompt else "",
                user_input=user_input,
                tool_specs=tool_specs,
                dispatch=dispatch,
                policy=envelope,
            )
        finally:
            await self._aclose_llm(llm)

        # A mid-loop HITL pause parks a resumable checkpoint; its id goes into the GATE step detail
        # (the engine correlates on the execution id, not this — it's for traceability, like a human
        # assignment). The transcript in the checkpoint is already redacted by the loop.
        steps = _serialize_steps(result.steps)
        if _is_hitl_pause(result):
            checkpoint = await self._checkpoints.create(
                organisation_id=org_id,
                execution_id=execution_id,
                manifest_doc=document,
                resume_messages=result.checkpoint.messages,
                pending_tool_calls=result.checkpoint.pending_tool_calls,
                approved_tool_call_id=result.checkpoint.approved_tool_call_id,
                resume_cursor=_cursor(result.checkpoint),
                redact_patterns=result.checkpoint.redact_patterns,
            )
            if steps and steps[-1]["kind"] == StepKind.GATE.value:
                steps[-1]["detail"] = str(checkpoint.id)

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
            total_tokens=result.total_tokens,
            steps=steps,
        )
        await self._emit_provenance(
            result.steps,
            result.status.value,
            org_id=str(org_id),
            principal=str(principal.principal_id),
            resource=resource,
        )
        await self._emit_consciousness(
            org_id=str(org_id),
            principal=str(principal.principal_id),
            resource=resource,
            harness_name=manifest.metadata.name,
            status=result.status.value,
            summary=result.output,
        )
        return row

    async def resume(
        self,
        *,
        execution_id: uuid.UUID,
        principal: Principal,
        decision: str,
        decision_reason: str | None = None,
    ) -> HarnessExecution:
        """Resolve a mid-loop HITL pause. APPROVED re-sources the exact paused manifest, rebuilds
        the runnable, and re-runs the loop from the checkpoint (the approved tool bypasses the gate
        once); ``DENIED`` terminates the run FAILED (``human_rejected``). Updates the SAME execution
        row in place and emits provenance only for the new step tail. Fail-closed throughout."""
        if principal.organisation_id is None:
            raise ResumeError("authenticated principal has no organisation scope", 401)
        org_id = principal.organisation_id
        execution = await self._executions.get(execution_id, org_id)
        if execution is None:
            raise ResumeError("execution not found", 404)
        if (
            execution.status != HarnessStatus.ESCALATED.value
            or execution.error_type != "hitl_required"
        ):
            raise ResumeError("execution is not awaiting HITL approval", 409)
        checkpoint = await self._checkpoints.get_latest_pending(execution_id, org_id)
        if checkpoint is None:
            raise ResumeError("no pending checkpoint to resume", 409)
        if decision not in ("APPROVED", "DENIED"):
            raise ResumeError("decision must be APPROVED or DENIED", 422)

        resource = f"harness_execution:{execution_id}"
        prov = str(principal.principal_id)
        # CAS the decision so a concurrent approve applies exactly once.
        if await self._checkpoints.set_decision(checkpoint.id, org_id, decision) is None:
            raise ResumeError("checkpoint already decided", 409)

        if decision == "DENIED":
            return await self._resume_denied(execution, org_id, prov, resource, decision_reason)
        return await self._resume_approved(execution, checkpoint, org_id, prov, resource)

    async def _resume_denied(
        self,
        execution: HarnessExecution,
        org_id: uuid.UUID,
        prov: str,
        resource: str,
        reason: str | None,
    ) -> HarnessExecution:
        prior = list(execution.steps or [])
        message = reason or "rejected by human"
        steps = [
            *prior,
            {
                "index": len(prior),
                "kind": StepKind.GATE.value,
                "name": "hitl",
                "status": "denied",
                "detail": message[:500],
            },
        ]
        row = await self._executions.update_run(
            execution.id,
            org_id,
            status=HarnessStatus.FAILED.value,
            output=execution.output,
            error_type="human_rejected",
            error_message=message,
            iterations=execution.iterations,
            total_tokens=execution.total_tokens,
            steps=steps,
        )
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=prov,
                action="human.reject",
                resource=resource,
                outcome="hitl:denied",
            )
        )
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=prov,
                action="harness.resume",
                resource=resource,
                outcome=HarnessStatus.FAILED.value,
            )
        )
        return row or execution

    async def _resume_approved(
        self,
        execution: HarnessExecution,
        checkpoint,  # noqa: ANN001 — the HarnessCheckpoint row
        org_id: uuid.UUID,
        prov: str,
        resource: str,
    ) -> HarnessExecution:
        # Replay the EXACT paused manifest (stored on the checkpoint → no drift, hash stable).
        manifest = load_ohm(checkpoint.manifest_doc)
        policy = resolve_policy_set(self._force_policy_set or manifest.governance.policy_set_ref)
        envelope, tool_specs, dispatch, llm = await self._build_runnable(manifest, policy, org_id)
        cursor = checkpoint.resume_cursor
        resume_state = LoopCheckpoint(
            messages=checkpoint.resume_messages,
            pending_tool_calls=checkpoint.pending_tool_calls,
            approved_tool_call_id=checkpoint.approved_tool_call_id,
            iteration=cursor["iteration"],
            tool_calls_made=cursor["tool_calls_made"],
            tokens_used=cursor["tokens_used"],
            redact_patterns=checkpoint.redact_patterns,
        )
        prompt = manifest.primary_prompt()
        try:
            result = await run_tool_use_loop(
                llm=llm,
                system=prompt.body if prompt else "",
                user_input=execution.input,
                tool_specs=tool_specs,
                dispatch=dispatch,
                policy=envelope,
                resume_state=resume_state,
            )
        finally:
            await self._aclose_llm(llm)

        # Append the NEW segment's steps to the prior trace; the loop reset its step list on resume,
        # so result.steps is the new tail only — provenance below emits only that, never the prefix.
        prior = list(execution.steps or [])
        new_steps = _serialize_steps(result.steps, base=len(prior))
        if _is_hitl_pause(result):  # a chained gate → park a fresh checkpoint
            cp2 = await self._checkpoints.create(
                organisation_id=org_id,
                execution_id=execution.id,
                manifest_doc=checkpoint.manifest_doc,
                resume_messages=result.checkpoint.messages,
                pending_tool_calls=result.checkpoint.pending_tool_calls,
                approved_tool_call_id=result.checkpoint.approved_tool_call_id,
                resume_cursor=_cursor(result.checkpoint),
                redact_patterns=result.checkpoint.redact_patterns,
            )
            if new_steps and new_steps[-1]["kind"] == StepKind.GATE.value:
                new_steps[-1]["detail"] = str(cp2.id)
        row = await self._executions.update_run(
            execution.id,
            org_id,
            status=result.status.value,
            output=result.output,
            error_type=result.error_type,
            error_message=result.error_message,
            iterations=result.iterations,  # cumulative — the cursor carried the prior iteration
            total_tokens=result.total_tokens,  # cumulative — the cursor seeded prior tokens
            steps=[*prior, *new_steps],
        )
        await self._emit_provenance(
            result.steps,  # the new tail only
            result.status.value,
            org_id=str(org_id),
            principal=prov,
            resource=resource,
            closure_action="harness.resume",
        )
        await self._emit_consciousness(
            org_id=str(org_id),
            principal=prov,
            resource=resource,
            harness_name=execution.harness_name,
            status=result.status.value,
            summary=result.output,
        )
        return row or execution

    async def _build_runnable(
        self,
        manifest,  # noqa: ANN001
        policy,  # noqa: ANN001
        org_id: uuid.UUID,
    ) -> tuple[Any, list[ToolSpec], Any, LLMClient]:
        """Resolve + materialise the manifest's capabilities, build the dispatch + the LLM + the
        runtime envelope. Shared by execute() and resume() so a resume sets up identically."""
        resolved = await self._resolve_all(manifest)
        envelope = build_envelope(manifest, policy, hard_max_iterations=self._max_iterations)
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

        llm = await self._build_llm(manifest, org_id)
        return envelope, tool_specs, dispatch, llm

    @staticmethod
    async def _aclose_llm(llm: LLMClient) -> None:
        aclose = getattr(llm, "aclose", None)
        if aclose is not None:
            await aclose()

    async def _dispatch_human(
        self,
        manifest,  # noqa: ANN001
        actor,  # noqa: ANN001
        org_id: uuid.UUID,
        principal: Principal,
        user_input: str,
        chash: str,
    ) -> HarnessExecution:
        """Human entrypoint actor → a task-board assignment + an ESCALATED run (resume is R5)."""
        execution_id = uuid.uuid4()
        resource = f"harness_execution:{execution_id}"
        human_role = actor.human_role or actor.role
        assignment = await self._assignments.create(
            organisation_id=org_id,
            execution_id=execution_id,
            harness_id=manifest.metadata.id,
            human_role=human_role,
            input_text=user_input,
        )
        row = await self._executions.create(
            execution_id=execution_id,
            organisation_id=org_id,
            user_id=principal.principal_id,
            harness_id=manifest.metadata.id,
            harness_name=manifest.metadata.name,
            content_hash=chash,
            status=HarnessStatus.ESCALATED.value,
            input_text=user_input,
            output=f"assigned to human role {human_role!r} (assignment {assignment.id})",
            error_type="human_assignment",
            error_message=f"awaiting human role {human_role!r}",
            iterations=0,
            total_tokens=0,
            steps=[
                {
                    "index": 0,
                    "kind": StepKind.GATE.value,
                    "name": human_role,
                    "status": "assigned",
                    "detail": str(assignment.id),
                }
            ],
        )
        prov = str(principal.principal_id)
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=prov,
                action="human.assign",
                resource=resource,
                outcome=f"{human_role}:assigned",
            )
        )
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=prov,
                action="harness.execute",
                resource=resource,
                outcome=HarnessStatus.ESCALATED.value,
            )
        )
        await self._emit_consciousness(
            org_id=str(org_id),
            principal=prov,
            resource=resource,
            harness_name=manifest.metadata.name,
            status=HarnessStatus.ESCALATED.value,
            summary=f"assigned to human role {human_role!r}",
        )
        return row

    async def _build_llm(self, manifest, org_id: uuid.UUID) -> LLMClient:  # noqa: ANN001
        """Build the loop's LLM client: the key-free fake, or a live client from the OHM's primary
        model + a BYOM key resolved via the broker (ADR-008 — no platform fallback)."""
        if self._llm_mode == "fake":
            return build_fake_client()
        model = manifest.primary_model()
        if model is None:
            raise HarnessExecutionError("live LLM mode requires a model in the OHM")
        provider, _, model_id = model.binding.partition("/")
        if not model_id:
            raise HarnessExecutionError(
                f"model binding {model.binding!r} must be '<provider>/<model-id>'"
            )
        base_url = self._llm_base_urls.get(provider)
        if not base_url:
            raise HarnessExecutionError(f"no base URL configured for LLM provider {provider!r}")
        credential_id = model.config.get("credential_id")
        if not credential_id:
            raise HarnessExecutionError("live LLM requires a BYOM key (model.config.credential_id)")
        try:
            payload = await self._broker.resolve_credential(
                credential_id=str(credential_id), organisation_id=org_id
            )
        except BrokerError as exc:
            raise HarnessExecutionError(f"model credential resolution failed: {exc}") from exc
        api_key = payload.get("api_key") or payload.get("key")
        if not api_key:
            raise HarnessExecutionError("the resolved model credential has no api_key")
        try:
            return build_live_client(
                protocol_shape=model.protocol_shape,
                base_url=base_url,
                api_key=str(api_key),
                model=model_id,
                timeout=self._llm_timeout,
            )
        except LLMConfigError as exc:
            raise HarnessExecutionError(str(exc)) from exc

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
        self,
        steps: list[LoopStep],
        status: str,
        *,
        org_id: str,
        principal: str,
        resource: str,
        closure_action: str = "harness.execute",
    ) -> None:
        """One provenance event per step + a closure event (the single write-through path). On a
        resume, ``steps`` is the new segment only (the loop reset its trace), so the replayed prefix
        is never re-emitted — preserving the per-step audit ordering across the pause."""
        _action = {
            StepKind.LLM: "llm.complete",
            StepKind.TOOL: "capability.invoke",
            StepKind.GATE: "governance.gate",
        }
        for step in steps:
            action = _action.get(step.kind, "capability.invoke")
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
                action=closure_action,
                resource=resource,
                outcome=status,
            )
        )

    async def _emit_consciousness(
        self,
        *,
        org_id: str,
        principal: str,
        resource: str,
        harness_name: str,
        status: str,
        summary: str | None,
    ) -> None:
        """Write-through a consciousness record (a provenance/event hook, NOT a privileged path) —
        captures the run's outcome so future interactions can retrieve it (the retrieval side is a
        later capability). Emitted via the same single provenance write path."""
        note = (summary or "").strip().replace("\n", " ")[:200] or "(no output)"
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=org_id,
                principal=principal,
                action="consciousness.write",
                resource=resource,
                outcome=f"{harness_name} → {status}: {note}",
            )
        )
