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

import logging
import uuid
from typing import Any

import yaml
from oraclous_governance import Principal
from oraclous_ohm.canonical import content_hash
from oraclous_ohm.errors import OHMParseError, OHMReferenceError
from oraclous_ohm.parse import load_ohm
from oraclous_ohm.references import resolve_capabilities
from oraclous_ohm.signatures import TrustStore, verify_signatures
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_harness_runtime_service.domain.llm.base import LLMClient, ToolSpec
from oraclous_harness_runtime_service.domain.llm.egress import (
    EgressBlockedError,
    validate_outbound_url,
)
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
from oraclous_harness_runtime_service.services.memory_client import MemoryWriter
from oraclous_harness_runtime_service.services.registry_client import RegistryClient, RegistryError

logger = logging.getLogger(__name__)

_RESERVED_CONFIG_KEYS = ("credential_mappings", "capability_id")

# Run states that count as a COMPLETED run for the post-run memory hook (#332 / ADR-027 §5) — an
# ESCALATED pause (HITL / human assignment) is not a completed run, so no memory is written for it.
_MEMORY_TERMINAL_STATUSES = (HarnessStatus.SUCCEEDED.value, HarnessStatus.FAILED.value)


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


def _primary_model_binding(manifest) -> str | None:  # noqa: ANN001
    """The OHM primary model's binding (the full ``<provider>/<model-id>`` string, e.g.
    ``openrouter/openai/gpt-4o-mini``) — recorded per execution so spend can be priced by model.
    ``None`` when the manifest declares no model (fake mode)."""
    model = manifest.primary_model()
    return model.binding if model is not None else None


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


def _manifest_graph_context(manifest) -> str | None:  # noqa: ANN001
    """The run's graph context (#332 / ADR-027 §5): when the manifest binds EXACTLY ONE distinct
    ``config.graph_id`` across its capabilities, that graph is where the run's memories land;
    zero or several → None (the KGS falls back to the org-default memory graph)."""
    try:
        graph_ids = {
            str(cap.config["graph_id"])
            for cap in manifest.capabilities
            if isinstance(cap.config, dict) and cap.config.get("graph_id")
        }
    except Exception:  # noqa: BLE001 — fail-soft: the hook never hurts a run
        return None
    return graph_ids.pop() if len(graph_ids) == 1 else None


def _tool_step_names(steps: list[LoopStep]) -> list[str]:
    """The TOOL-step names for the memory hook. Fail-soft: a future shape change must never let
    this raise into the run path (the hook is best-effort), so any error yields no names."""
    try:
        return [s.name for s in steps if s.kind is StepKind.TOOL and s.name]
    except Exception:  # noqa: BLE001 — fail-soft: the hook never hurts a run
        return []


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
        llm_allow_private: bool,
        max_iterations: int,
        memory: MemoryWriter | None = None,
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
        self._llm_allow_private = llm_allow_private
        self._max_iterations = max_iterations
        # The post-run memory hook (#332 / ADR-027 §5). None when HARNESS_MEMORY_WRITES is off
        # (the code default) — flag off means ZERO memory calls, not a no-op writer.
        self._memory = memory

    async def execute(
        self,
        *,
        manifest_inline: str | dict[str, Any] | None,
        manifest_ref: str | None,
        user_input: str,
        principal: Principal,
        capability_ceiling: list[str] | None = None,
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

        # cap the ceiling by the caller's member tools[] (ADR-032/035 §5) — fail-closed for a
        # manifest_ref member too, whose registered manifest could otherwise declare a broader set.
        ext_ceiling = frozenset(capability_ceiling) if capability_ceiling is not None else None
        envelope, tool_specs, dispatch, llm = await self._build_runnable(
            manifest, policy, org_id, external_ceiling=ext_ceiling
        )
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
        cp = result.checkpoint
        # cp is not None whenever _is_hitl_pause holds (it just narrows cp for the create below)
        if _is_hitl_pause(result) and cp is not None:
            checkpoint = await self._checkpoints.create(
                organisation_id=org_id,
                execution_id=execution_id,
                manifest_doc=document,
                resume_messages=cp.messages,
                pending_tool_calls=cp.pending_tool_calls,
                approved_tool_call_id=cp.approved_tool_call_id,
                resume_cursor=_cursor(cp),
                redact_patterns=cp.redact_patterns,
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
            model=_primary_model_binding(manifest),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
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
        # Post-run memory hook (#332 / ADR-027 §5): fire-and-forget AFTER the run is fully
        # persisted + audited — it can never fail, block, or slow the run. The ENTIRE block (arg
        # construction + schedule) is inside the swallow-all guard so that even an arg expression
        # (_tool_step_names / _manifest_graph_context / metadata access) raising on a future shape
        # change can never 500 an already-SUCCEEDED, already-persisted run.
        try:
            self._write_run_memories(
                harness_id=str(manifest.metadata.id),
                harness_name=manifest.metadata.name,
                status=result.status.value,
                user_input=user_input,
                output=result.output,
                tool_names=_tool_step_names(result.steps),
                execution_id=execution_id,
                graph_id=_manifest_graph_context(manifest),
            )
        except Exception:  # noqa: BLE001 — the run is done; the memory hook can never undo it
            logger.warning("post-run memory hook failed; run unaffected")
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

        # If applying the decision then fails (e.g. the registry/LLM is transiently down while
        # rebuilding the runnable), un-claim the checkpoint so the run stays retryable rather than
        # stranded ESCALATED with a no-longer-PENDING checkpoint. (A failure AFTER the loop already
        # dispatched a tool may re-run it on retry — the common failure is pre-loop in build.)
        try:
            if decision == "DENIED":
                return await self._resume_denied(execution, org_id, prov, resource, decision_reason)
            return await self._resume_approved(
                execution, checkpoint, org_id, prov, resource, decision_reason
            )
        except Exception:
            await self._checkpoints.revert_to_pending(checkpoint.id, org_id)
            raise

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
        # Post-run memory hook: the run COMPLETED (FAILED, human_rejected) and the denial reason is
        # explicit human feedback. No manifest is loaded on this path → no graph context (the KGS
        # org-default memory graph applies). The whole block is guarded so no arg expression can
        # raise into a run that has already FAILED-and-persisted.
        try:
            self._write_run_memories(
                harness_id=str(getattr(execution, "harness_id", "") or ""),
                harness_name=execution.harness_name,
                status=HarnessStatus.FAILED.value,
                user_input=execution.input,
                output=message,
                tool_names=[],
                execution_id=execution.id,
                graph_id=None,
                human_feedback=reason,
            )
        except Exception:  # noqa: BLE001 — the run is done; the memory hook can never undo it
            logger.warning("post-run memory hook failed; run unaffected")
        return row or execution

    async def _resume_approved(
        self,
        execution: HarnessExecution,
        checkpoint,  # noqa: ANN001 — the HarnessCheckpoint row
        org_id: uuid.UUID,
        prov: str,
        resource: str,
        decision_reason: str | None = None,
    ) -> HarnessExecution:
        # Replay the EXACT paused manifest (stored on the checkpoint → no drift, hash stable).
        document = checkpoint.manifest_doc
        manifest = load_ohm(document)
        policy = resolve_policy_set(self._force_policy_set or manifest.governance.policy_set_ref)
        # Re-enforce the load-time gates on resume: the manifest can't have drifted, but the policy
        # floor may have tightened since the pause — a now-forbidden capability/provider/signature
        # must fail-closed here exactly as a fresh execute() would, not slip through on resume.
        verify_signatures(
            document, self._trust, require=self._require_signature or policy.require_signature
        )
        enforce_load_policy(manifest, policy)
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
        new_cp = result.checkpoint
        # a chained gate → park a fresh checkpoint (new_cp is not None whenever _is_hitl_pause)
        if _is_hitl_pause(result) and new_cp is not None:
            cp2 = await self._checkpoints.create(
                organisation_id=org_id,
                execution_id=execution.id,
                manifest_doc=checkpoint.manifest_doc,
                resume_messages=new_cp.messages,
                pending_tool_calls=new_cp.pending_tool_calls,
                approved_tool_call_id=new_cp.approved_tool_call_id,
                resume_cursor=_cursor(new_cp),
                redact_patterns=new_cp.redact_patterns,
            )
            if new_steps and new_steps[-1]["kind"] == StepKind.GATE.value:
                new_steps[-1]["detail"] = str(cp2.id)
        row = await self._executions.update_run(
            execution.id,
            org_id,
            status=result.status.value,
            output=result.output
            or execution.output,  # keep the prior partial on a chained re-pause
            error_type=result.error_type,
            error_message=result.error_message,
            iterations=result.iterations,  # cumulative — the cursor carried the prior iteration
            total_tokens=result.total_tokens,  # cumulative — the cursor seeded prior tokens
            # The cursor carries only the cumulative total, so the loop's input/output reflect this
            # segment only; fold them into the prior persisted split to keep the breakdown
            # cumulative.
            input_tokens=(execution.input_tokens or 0) + result.input_tokens,
            output_tokens=(execution.output_tokens or 0) + result.output_tokens,
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
        # Post-run memory hook: only a COMPLETED run writes (a chained re-pause does not); an
        # approval carrying an explicit reason is human feedback worth a procedural memory. The
        # whole block is guarded so no arg expression can raise into an already-persisted run.
        try:
            self._write_run_memories(
                harness_id=str(manifest.metadata.id),
                harness_name=execution.harness_name,
                status=result.status.value,
                user_input=execution.input,
                output=result.output,
                tool_names=_tool_step_names(result.steps),
                execution_id=execution.id,
                graph_id=_manifest_graph_context(manifest),
                human_feedback=decision_reason,
            )
        except Exception:  # noqa: BLE001 — the run is done; the memory hook can never undo it
            logger.warning("post-run memory hook failed; run unaffected")
        return row or execution

    def _write_run_memories(
        self,
        *,
        harness_id: str,
        harness_name: str,
        status: str,
        user_input: str,
        output: str | None,
        tool_names: list[str],
        execution_id: uuid.UUID,
        graph_id: str | None,
        human_feedback: str | None = None,
    ) -> None:
        """The flag-gated, fail-soft post-run memory hook (#332 / ADR-027 §5).

        No writer (flag off) → ZERO calls. A completed run (SUCCEEDED/FAILED) schedules one
        episodic outcome memory; explicit human feedback additionally schedules a procedural one.
        Scheduling is fire-and-forget (≈2s-timeout detached tasks) and everything is swallowed —
        this method can never raise into the run path.
        """
        if self._memory is None or status not in _MEMORY_TERMINAL_STATUSES:
            return
        try:
            self._memory.schedule_run_outcome(
                harness_id=harness_id,
                harness_name=harness_name,
                status=status,
                user_input=user_input,
                output=output,
                tool_names=tool_names,
                execution_id=execution_id,
                graph_id=graph_id,
            )
            if human_feedback and human_feedback.strip():
                self._memory.schedule_human_feedback(
                    harness_id=harness_id,
                    harness_name=harness_name,
                    feedback=human_feedback,
                    execution_id=execution_id,
                    graph_id=graph_id,
                )
        except Exception:  # noqa: BLE001 — belt-and-braces; the writer already swallows everything
            logger.warning("post-run memory hook failed to schedule; run unaffected")

    async def _build_runnable(
        self,
        manifest,  # noqa: ANN001
        policy,  # noqa: ANN001
        org_id: uuid.UUID,
        *,
        external_ceiling: frozenset[str] | None = None,
    ) -> tuple[Any, list[ToolSpec], Any, LLMClient]:
        """Resolve + materialise the manifest's capabilities, build the dispatch + the LLM + the
        runtime envelope. Shared by execute() and resume() so a resume sets up identically.
        ``external_ceiling`` caps the ceiling by the caller's member ``tools[]`` (ADR-032)."""
        resolved = await self._resolve_all(manifest)
        envelope = build_envelope(
            manifest,
            policy,
            hard_max_iterations=self._max_iterations,
            external_ceiling=external_ceiling,
        )
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

        # Resolve the base URL: the CONNECTION's own `base_url` (a custom OpenAI-compatible
        # endpoint, e.g. a local or self-hosted LLM) wins; otherwise fall back to the operator's
        # server map keyed by provider (openrouter/openai). A USER-supplied URL is attacker-
        # controllable → run it through the egress guard (SSRF). The operator's server-map URLs are
        # TRUSTED and are NOT guarded.
        connection_base_url = payload.get("base_url")
        base_url = connection_base_url or self._llm_base_urls.get(provider)
        if not base_url:
            raise HarnessExecutionError(
                f"no base URL for provider {provider!r} — set base_url on the connection or "
                "configure the provider"
            )
        if connection_base_url:
            try:
                validate_outbound_url(
                    str(connection_base_url), allow_private=self._llm_allow_private
                )
            except EgressBlockedError as exc:
                raise HarnessExecutionError(f"connection base_url rejected: {exc}") from exc
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
