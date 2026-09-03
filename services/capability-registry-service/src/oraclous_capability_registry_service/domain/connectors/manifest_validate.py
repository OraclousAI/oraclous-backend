"""manifest-validate connector (domain layer) — the compiler reviewer's deterministic validate gate.

Issue #594 / ADR-047. A first-party, org-scoped, credential-free INTERNAL tool the compiler's
``reviewer`` member OPTS INTO via its OHM toolset as ``core/manifest-validate@1``. It wraps the ohm
validator ``validate_draft``: given a drafted Team Harness (the drafter's JSON — possibly wrapped in
the LLM's prose / a ```json fence) plus the surveyed tool catalog, it runs the SAME
``assemble_and_report`` dry-run the importer uses (one validator, two on-ramps) and returns a CODED
``would_block`` verdict with the blocking reasons. The verdict is deterministic CODE, never the
model's opinion (ADR-043 invariant). No network and no credential — the validation is pure and
in-process; only the caller's verified org id (from the execution context) is used.

#705 — THE ALLOWED SET IS READ, NOT RELAYED. The gate sources the org's registered TOOL descriptors
from the capability repository itself, by code, on every call. It used to union whatever ``catalog``
the ``reviewer`` member typed into the tool call; an imported MCP tool is a descriptor ROW, not an
in-process plugin, so that relay was its only source, and one name the model dropped while re-typing
a 72-entry list blocked a whole compile. A deterministic validator fed a model-authored fact is not
deterministic. The repository is injected by the SERVICES layer at execute time (this is a domain
object and never touches the database itself), exactly as ``GitHubSinkConnector`` gets its
``delivery_repo``.

#708 factored the read itself out to ``_catalog.read_allowed_catalog``, shared with
``ManifestRefineConnector`` — the identical relay bug #705 fixed here, found again in the refine
gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

if TYPE_CHECKING:
    from oraclous_capability_registry_service.repositories.capability_repository import (
        CapabilityRepository,
    )


class ManifestValidateConnector(InternalTool):
    """Wraps ohm ``validate_draft`` as a registry tool — the reviewer's capability-absence gate."""

    #: the org's registered capabilities, injected on the LIVE path by ToolExecutionService. None on
    #: a unit construction / a degraded start → the built-ins-only floor below (never fail-open).
    capability_repo: CapabilityRepository | None = None

    async def _allowed_catalog(self, context: ExecutionContext) -> list[str]:
        """The tools the calling org may actually draw from — READ, never relayed.

        #708 factored the actual logic out to ``_catalog.read_allowed_catalog``, shared with
        ``ManifestRefineConnector`` — kept as a thin wrapper here for callers/tests that reach it by
        this name."""
        from oraclous_capability_registry_service.domain.connectors._catalog import (
            read_allowed_catalog,
        )

        return await read_allowed_catalog(self.capability_repo, context.organisation_id)

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
        # THE DETERMINISTIC REGISTRY-DIFF (ADR-047): a tool is "available" iff it is REGISTERED to
        # the calling org. Read here, by code — an ``input_data["catalog"]`` is ignored entirely,
        # whether it is absent, partial or falsely inflated. A tool that is registered nowhere
        # still blocks F-CAPABILITY-MISSING (ADR-032).
        catalog = await self._allowed_catalog(context)
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
