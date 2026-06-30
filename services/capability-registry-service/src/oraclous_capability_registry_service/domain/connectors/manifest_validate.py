"""manifest-validate connector (domain layer) — the compiler reviewer's deterministic validate gate.

Issue #594 / ADR-047. A first-party, org-scoped, credential-free INTERNAL tool the compiler's
``reviewer`` member OPTS INTO via its OHM toolset as ``core/manifest-validate@1``. It wraps the ohm
validator ``validate_draft``: given a drafted Team Harness (the drafter's JSON — possibly wrapped in
the LLM's prose / a ```json fence) plus the surveyed tool catalog, it runs the SAME
``assemble_and_report`` dry-run the importer uses (one validator, two on-ramps) and returns a CODED
``would_block`` verdict with the blocking reasons. The verdict is deterministic CODE, never the
model's opinion (ADR-043 invariant). No network and no credential — the validation is pure and
in-process; only the caller's verified org id (from the execution context) is used.
"""

from __future__ import annotations

from typing import Any

from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)


class ManifestValidateConnector(InternalTool):
    """Wraps ohm ``validate_draft`` as a registry tool — the reviewer's capability-absence gate."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        # function-local import: oraclous_ohm is the shared kernel (a registry → ohm import is
        # allowed by the import contract — only ohm → services is forbidden), kept local so test
        # collection never depends on ohm being importable at module import time.
        from oraclous_ohm.compiler import validate_draft

        draft = input_data.get("draft")
        if draft is None or (isinstance(draft, str) and not draft.strip()):
            return ExecutionResult(
                success=False,
                error_message="'draft' is required",
                error_type="INVALID_INPUT",
            )
        # THE DETERMINISTIC REGISTRY-DIFF (ADR-047): a tool is "available" iff it is REGISTERED. We
        # source the allowed set from the live plugin registry (the seeded built-ins) and UNION it
        # with whatever catalog the reviewer relayed — so a legitimately registered tool is
        # never falsely blocked when the reviewer (an LLM) does not relay the surveyed catalog
        # verbatim. The gate is a CODED diff against reality, not the model's relay; a hallucinated
        # tool that is neither surveyed NOR registered still blocks F-CAPABILITY-MISSING (ADR-032).
        from oraclous_capability_registry_service.domain.plugins import plugin_registry

        relayed = input_data.get("catalog")
        passed = (
            relayed.get("tools", [])
            if isinstance(relayed, dict)
            else (relayed if isinstance(relayed, list) else [])
        )
        # use the public descriptor() contract (metadata.name) — discover() is typed to the base
        registered = [str(p.descriptor()["metadata"]["name"]) for p in plugin_registry.discover()]
        catalog = [*passed, *registered]
        try:
            verdict = validate_draft(draft, catalog, owner_organization_id=context.organisation_id)
        except Exception:  # noqa: BLE001 — FAIL CLOSED: any internal failure is a BLOCK, never a
            # result the reviewer could read as "not blocked" (validate_draft should never raise,
            # but the gate must never green-light a draft it could not actually validate).
            verdict = {
                "would_block": True,
                "blocking": ["F-VALIDATOR-ERROR: the draft could not be validated"],
                "report": "GO: BLOCKED — the validator failed; fail-closed.",
            }
        # the TOOL CALL succeeded (the validation RAN) even when the draft is blocked — would_block
        # is part of the result the reviewer reads to decide re-draft vs emit, NOT a tool failure.
        return ExecutionResult(
            success=True,
            data=verdict,  # {"would_block": bool, "blocking": [...], "report": str}
            metadata={
                "would_block": bool(verdict.get("would_block")),
                "blocking_count": len(verdict.get("blocking", [])),
            },
        )
