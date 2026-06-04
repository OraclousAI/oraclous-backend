"""Per-harness capability allocation service — ORAA-77 / T2-M3.

Exports:
  HarnessCapabilityAllocator  — async allocate() and list_allocations_for_harness()
  ScopeViolationError         — raised when a capability's oauth scope exceeds the harness scope
  InvocationHandle            — returned per allocated capability
  check_scope_compliance      — pure T2-M3 scope-check function
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class ScopeViolationError(Exception):
    """Raised when a capability's credential scope exceeds the harness's declared scope (T2-M3)."""

    def __init__(
        self,
        harness_id: uuid.UUID,
        capability_id: uuid.UUID,
        violating_scopes: list[str],
    ) -> None:
        self.harness_id = harness_id
        self.capability_id = capability_id
        self.violating_scopes = violating_scopes
        super().__init__(
            f"Harness {harness_id} scope violation for capability {capability_id}: "
            f"unauthorized scopes {violating_scopes!r}"
        )


@dataclass
class InvocationHandle:
    """Returned per allocated capability — carries enough context to invoke at runtime."""

    capability_id: uuid.UUID
    kind: Any  # DescriptorKind — imported function-locally to avoid circular imports at collection
    descriptor: dict[str, Any]


def check_scope_compliance(
    harness_id: uuid.UUID,
    capability_id: uuid.UUID,
    harness_declared_scope: list[str],
    credential_requirements: list[dict[str, Any]],
) -> None:
    """Pure T2-M3 scope-compliance check.

    Raises ScopeViolationError if any oauth_token requirement contains a scope not
    declared by the harness.  Non-oauth credential types (api_key, connection_string,
    username_password) are not scope-checked.

    Fail-closed: raises on the first violation found.
    """
    harness_scope_set = set(harness_declared_scope)
    for req in credential_requirements:
        if req.get("type") != "oauth_token":
            continue
        required_scopes: list[str] = req.get("scopes") or []
        violating = [s for s in required_scopes if s not in harness_scope_set]
        if violating:
            raise ScopeViolationError(
                harness_id=harness_id,
                capability_id=capability_id,
                violating_scopes=violating,
            )


class HarnessCapabilityAllocator:
    """Allocates capabilities to a harness subject to T2-M3 scope enforcement."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def allocate(
        self,
        org_id: uuid.UUID,
        harness_id: uuid.UUID,
        capability_ids: list[uuid.UUID],
    ) -> list[InvocationHandle]:
        """Allocate a set of capabilities to a harness and return invocation handles.

        Atomic: if any capability fails the T2-M3 scope check the entire batch is
        rejected — no partial allocations are written.

        Raises ScopeViolationError on the first T2-M3 violation.
        """
        from app.repositories.capability_descriptor_repository import (
            CapabilityDescriptorRepository,
        )
        from app.repositories.harness_capability_allocation_repository import (
            HarnessCapabilityAllocationRepository,
        )

        cap_repo = CapabilityDescriptorRepository(self._session)
        alloc_repo = HarnessCapabilityAllocationRepository(self._session)

        harness_row = await cap_repo.get_by_id(harness_id)
        harness_declared_scope: list[str] = (
            harness_row.descriptor["spec"].get("credential_scope", [])
            if harness_row is not None
            else []
        )

        # Phase 1: scope-check every capability before writing — ensures atomicity
        capability_rows = []
        for cap_id in capability_ids:
            cap_row = await cap_repo.get_by_id(cap_id)
            cred_reqs: list[dict[str, Any]] = (
                cap_row.descriptor["spec"].get("credential_requirements", [])
                if cap_row is not None
                else []
            )
            check_scope_compliance(harness_id, cap_id, harness_declared_scope, cred_reqs)
            capability_rows.append(cap_row)

        # Phase 2: all checks passed — persist allocations and build handles
        handles: list[InvocationHandle] = []
        for cap_row in capability_rows:
            await alloc_repo.create(
                org_id=org_id,
                harness_id=harness_id,
                capability_id=cap_row.id,
            )
            handles.append(
                InvocationHandle(
                    capability_id=cap_row.id,
                    kind=cap_row.kind,
                    descriptor=cap_row.descriptor,
                )
            )
        return handles

    async def list_allocations_for_harness(
        self,
        org_id: uuid.UUID,
        harness_id: uuid.UUID,
    ) -> list:
        """Return all allocations for this harness within the given org.

        Returns [] for a harness with no allocations (or a harness_id from a different org).
        """
        from app.repositories.harness_capability_allocation_repository import (
            HarnessCapabilityAllocationRepository,
        )

        alloc_repo = HarnessCapabilityAllocationRepository(self._session)
        return await alloc_repo.list_by_harness(org_id=org_id, harness_id=harness_id)
