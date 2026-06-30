"""manifest-refine connector (domain layer) — apply a typed NL-refine op + re-validate.

Issue #595 / ADR-047 §4. The compiler's refine member (or any caller) emits ONE typed structural op
(add_member / set_fan_out / change_kind / add_depends_on — the small, reliable function-calling
shape); this DETERMINISTIC tool applies it to the supplied manifest via ohm ``apply_refine`` and
re-validates through the SAME ``assemble_and_report`` dry-run the importer/compiler use (one
validator). The manifest flows in deterministically (NOT re-emitted by the model) so the
PRESERVE-THE-REST byte-identity invariant holds. A delta that cycles the DAG, references an
unsurveyed tool, or breaks a member schema is rejected (``would_block=True``, ``applied=False``,
``manifest=None``) — never silently applied; capability cannot be escalated (ADR-032). No network,
no credential; the surveyed catalog is unioned with the live registry (the deterministic
registry-diff, like manifest-validate).
"""

from __future__ import annotations

from typing import Any

from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)


class ManifestRefineConnector(InternalTool):
    """Wraps ohm ``apply_refine`` as a registry tool — the deterministic NL-refine op applier."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        # function-local: oraclous_ohm is the shared kernel (a registry → ohm import is allowed).
        from oraclous_ohm.compiler import apply_refine, parse_op
        from oraclous_ohm.parse import load_ohm

        from oraclous_capability_registry_service.domain.plugins import plugin_registry

        raw_manifest = input_data.get("manifest")
        raw_op = input_data.get("edit_op")
        if not raw_manifest:
            return ExecutionResult(
                success=False, error_message="'manifest' is required", error_type="INVALID_INPUT"
            )
        if not raw_op:
            return ExecutionResult(
                success=False, error_message="'edit_op' is required", error_type="INVALID_INPUT"
            )

        # the deterministic registry-diff catalog (same as manifest-validate): a tool is available
        # iff surveyed OR registered, so a refine cannot escalate to an unregistered capability.
        relayed = input_data.get("catalog")
        passed = (
            relayed.get("tools", [])
            if isinstance(relayed, dict)
            else (relayed if isinstance(relayed, list) else [])
        )
        registered = [str(p.descriptor()["metadata"]["name"]) for p in plugin_registry.discover()]
        catalog = [*passed, *registered]

        try:
            manifest = load_ohm(raw_manifest)
            op = parse_op(raw_op)
        except Exception as exc:  # noqa: BLE001 — FAIL CLOSED: a bad manifest/op blocks, never applies
            return ExecutionResult(
                success=True,
                data={
                    "would_block": True,
                    "applied": False,
                    "blocking": [f"F-REFINE-INPUT: {exc}"],
                    "manifest": None,
                },
                metadata={"would_block": True},
            )

        result = apply_refine(
            manifest, op, catalog=catalog, owner_organization_id=context.organisation_id
        )
        return ExecutionResult(
            success=True,
            data={
                "would_block": result.report.would_block,
                "applied": result.manifest is not None,
                "blocking": result.report.blocking,
                # the PATCHED manifest (only the named member changed) — None if the delta blocked.
                "manifest": result.manifest.model_dump(mode="json") if result.manifest else None,
            },
            metadata={
                "would_block": result.report.would_block,
                "applied": result.manifest is not None,
            },
        )
