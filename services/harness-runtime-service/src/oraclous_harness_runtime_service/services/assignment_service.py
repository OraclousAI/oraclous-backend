"""Human-actor assignment lifecycle (services layer).

R4 (S5) creates a PENDING ``harness_assignments`` row when a human is the entrypoint actor and parks
the run ESCALATED. This service is the claim/complete round-trip the execution-engine (R5 S4) drives
over HTTP: a human claims the task, then submits their output — which marks the assignment COMPLETED
and flips the parked execution ESCALATED → SUCCEEDED with that output. Org from the principal only
(ADR-006, fail-closed); a provenance event per transition (§3.7).
"""

from __future__ import annotations

import uuid

from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_harness_runtime_service.models.assignment import HarnessAssignment
from oraclous_harness_runtime_service.models.enums import HarnessStatus
from oraclous_harness_runtime_service.repositories.assignment_repository import AssignmentRepository
from oraclous_harness_runtime_service.repositories.execution_repository import ExecutionRepository


class AssignmentError(Exception):
    """The assignment could not be transitioned (missing / wrong state). Maps to HTTP 404/409."""


class AssignmentService:
    def __init__(
        self,
        *,
        assignments: AssignmentRepository,
        executions: ExecutionRepository,
        provenance: ProvenanceCollector,
    ) -> None:
        self._assignments = assignments
        self._executions = executions
        self._provenance = provenance

    async def claim(self, assignment_id: uuid.UUID, principal: Principal) -> HarnessAssignment:
        org_id = self._require_org(principal)
        row = await self._assignments.claim(assignment_id, org_id)
        if row is None:
            raise AssignmentError("assignment not found or not claimable (must be PENDING)")
        await self._emit(org_id, principal, row, "human.claim", "CLAIMED")
        return row

    async def complete(
        self, assignment_id: uuid.UUID, principal: Principal, output: str
    ) -> HarnessAssignment:
        org_id = self._require_org(principal)
        row = await self._assignments.complete(assignment_id, org_id)
        if row is None:
            raise AssignmentError("assignment not found or already completed")
        # flip the parked run ESCALATED → SUCCEEDED with the human's output.
        await self._executions.update_status(
            row.execution_id, org_id, status=HarnessStatus.SUCCEEDED.value, output=output
        )
        await self._emit(org_id, principal, row, "human.complete", "COMPLETED")
        return row

    @staticmethod
    def _require_org(principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:
            raise AssignmentError("authenticated principal has no organisation scope")
        return principal.organisation_id

    async def _emit(
        self,
        org_id: uuid.UUID,
        principal: Principal,
        row: HarnessAssignment,
        action: str,
        outcome: str,
    ) -> None:
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=str(principal.principal_id),
                action=action,
                resource=f"harness_execution:{row.execution_id}",
                outcome=f"{row.human_role}:{outcome}",
            )
        )
