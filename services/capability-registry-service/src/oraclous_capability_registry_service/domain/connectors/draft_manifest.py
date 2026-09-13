"""draft-manifest connector (domain layer) — the compiler drafter's own structured answer.

Issue #900 / ADR-053. A first-party, org-scoped, credential-free INTERNAL tool the compiler's
``manifest-drafter`` member is given as its structured-answer tool, ``core/draft-manifest@1``.
Unlike ``manifest-validate`` (whose caller, the reviewer, relays a manifest it did NOT author,
wrapped as ``{"draft": ...}``), this connector's own ``input_data`` dict IS the drafted OHM Team
Harness directly — the tool call's arguments ARE the drafter's answer (ADR-053 decision 2;
``run_tool_use_loop`` turns ``ToolCall.args`` straight into the run's output). There is no wrapper
key to unwrap.

It still wraps ohm ``validate_draft`` — the SAME ``assemble_and_report`` dry-run
``ManifestValidateConnector`` runs — so a drafted manifest gets the identical deterministic,
CODED ``would_block`` verdict before it is ever accepted as the drafter's answer (ADR-043
invariant: the verdict is code, never the model's opinion).

#705's rule, inherited unchanged: THE ALLOWED SET IS READ, NOT RELAYED. The gate sources the org's
registered TOOL descriptors from the capability repository itself, by code, on every call — never
from anything the model put in its own call arguments. The repository is injected by the SERVICES
layer at execute time (this is a domain object and never touches the database itself), exactly as
``ManifestValidateConnector`` gets its ``capability_repo``.
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


class DraftManifestConnector(InternalTool):
    """Wraps ohm ``validate_draft`` as the drafter's own answer-tool — the call's arguments ARE
    the drafted Team Harness, validated before being accepted as the answer."""

    #: the org's registered capabilities, injected on the LIVE path by ToolExecutionService. None on
    #: a unit construction / a degraded start → the built-ins-only floor below (never fail-open).
    capability_repo: CapabilityRepository | None = None

    async def _allowed_catalog(self, context: ExecutionContext) -> list[str]:
        """The tools the calling org may actually draw from — READ, never relayed.

        Shares the exact reader ``ManifestValidateConnector``/``ManifestRefineConnector`` use, so
        the three gates can never drift apart (see ``_catalog.read_allowed_catalog``)."""
        from oraclous_capability_registry_service.domain.connectors._catalog import (
            read_allowed_catalog,
        )

        return await read_allowed_catalog(self.capability_repo, context.organisation_id)

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        # function-local import: oraclous_ohm is the shared kernel (a registry → ohm import is
        # allowed by the import contract — only ohm → services is forbidden), kept local so test
        # collection never depends on ohm being importable at module import time, and so a test
        # monkeypatching ``oraclous_ohm.compiler.validate_draft`` is picked up at call time.
        from oraclous_ohm.compiler import validate_draft

        # input_data IS the drafted manifest directly — no "draft" key to unwrap (see module
        # docstring). InternalTool.execute() already refused a non-dict input_data before this
        # method ever ran.
        catalog = await self._allowed_catalog(context)
        try:
            verdict = validate_draft(
                input_data, catalog, owner_organization_id=context.organisation_id
            )
        except Exception:  # noqa: BLE001 — FAIL CLOSED: any internal failure is a BLOCK, never a
            # result the drafter/reviewer could read as "not blocked" (validate_draft should never
            # raise, but the gate must never green-light a draft it could not actually validate).
            verdict = {
                "would_block": True,
                "blocking": ["F-VALIDATOR-ERROR: the draft could not be validated"],
                "report": "GO: BLOCKED — the validator failed; fail-closed.",
            }
        # the TOOL CALL succeeded (the validation RAN) even when the draft is blocked — would_block
        # is part of the result the drafter's answer carries, NOT a tool failure. A blocked verdict
        # must still be a successful StepKind.TOOL step (ADR-053 decision 3's receipt only mints on
        # success).
        return ExecutionResult(
            success=True,
            data=verdict,  # {"would_block": bool, "blocking": [...], "report": str}
            metadata={
                "would_block": bool(verdict.get("would_block")),
                "blocking_count": len(verdict.get("blocking", [])),
            },
        )
