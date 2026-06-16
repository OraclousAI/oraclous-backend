"""Harness↔graph binding use-cases (ORAA-4 §21 services layer; ADR-029 / Contract §G2).

The authority for the workspace↔harness curation edge. Orchestrates the org-scoped binding
repository with two visibility checks (ADR-029 §3, both sides verified):

* **Harness** — resolved via the registry's own org-scoped lookup
  (``CapabilityRepository.get_by_id``), which admits the caller's org OR the shared ``PLATFORM_ORG``
  (so a shared/platform agent is bindable). A harness that is not a ``kind:harness`` capability, or
  not visible to the caller, is a 404 (mask).
* **Graph** — the registry has no graph table and KGS exposes no graph org, so membership is
  verified via a KGS internal call (``GraphMembershipClient``). On attach the target ``graph_id``
  must be in the caller's accessible set (absent → 404). The read paths reuse that set to filter +
  name the surviving ``graph_id``s, skipping a dangling row whose graph was deleted (ADR-029 §4).

Curation only (ADR-029 §2): a binding grants NO data access and changes NO execution route. Every
call carries the ``organisation_id`` from the authenticated principal (ORG001 — never the request
body); ``attach`` stamps ``created_by`` from the principal's user id. There is no 409 — a
not-visible object is a 404 and an idempotent re-attach is a 200 (ADR-029 §6).
"""

from __future__ import annotations

import uuid

from oraclous_capability_registry_service.domain.errors import CapabilityNotFoundError
from oraclous_capability_registry_service.domain.manifest import (
    descriptor_name,
    descriptor_summary,
)
from oraclous_capability_registry_service.models.enums import DescriptorKind
from oraclous_capability_registry_service.repositories.binding_repository import BindingRepository
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.schema.binding_schema import (
    BoundAgent,
    BoundGraph,
    CreateBinding,
)
from oraclous_capability_registry_service.services.graph_membership_client import (
    GraphMembershipClient,
)


class BindingService:
    def __init__(
        self,
        *,
        bindings: BindingRepository,
        capabilities: CapabilityRepository,
        graphs: GraphMembershipClient,
    ) -> None:
        self._bindings = bindings
        self._capabilities = capabilities
        self._graphs = graphs

    async def attach(
        self, *, body: CreateBinding, organisation_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Bind a harness to a graph. Returns ``created`` (True new / False idempotent re-attach).

        Both sides are verified visible to the caller's org (else 404, masked): the harness via the
        org-scoped registry lookup (caller-org OR PLATFORM_ORG), the graph via the KGS membership.
        """
        harness = await self._capabilities.get_by_id(body.harness_id, organisation_id)
        if harness is None or DescriptorKind(harness.kind) is not DescriptorKind.HARNESS:
            raise CapabilityNotFoundError("agent not found")
        accessible = await self._graphs.accessible_graphs(
            organisation_id=organisation_id, user_id=user_id
        )
        if body.graph_id not in accessible:
            raise CapabilityNotFoundError("workspace not found")
        _row, created = await self._bindings.attach(
            organisation_id=organisation_id,
            harness_capability_id=body.harness_id,
            graph_id=body.graph_id,
            created_by=user_id,
        )
        return created

    async def detach(
        self, *, organisation_id: uuid.UUID, harness_id: uuid.UUID, graph_id: uuid.UUID
    ) -> None:
        """Remove the binding; a missing / cross-org pair is a 404 (mask)."""
        if not await self._bindings.detach(
            organisation_id=organisation_id,
            harness_capability_id=harness_id,
            graph_id=graph_id,
        ):
            raise CapabilityNotFoundError("binding not found")

    async def list_by_graph(
        self, *, organisation_id: uuid.UUID, user_id: uuid.UUID, graph_id: uuid.UUID
    ) -> list[BoundAgent]:
        """The agents bound to a workspace. Returns ``[]`` when the workspace is not visible to the
        caller (a not-visible graph yields no rows — the same mask as a 404, without leaking)."""
        accessible = await self._graphs.accessible_graphs(
            organisation_id=organisation_id, user_id=user_id
        )
        if graph_id not in accessible:
            return []
        out: list[BoundAgent] = []
        for binding in await self._bindings.list_by_graph(
            organisation_id=organisation_id, graph_id=graph_id
        ):
            harness = await self._capabilities.get_by_id(
                binding.harness_capability_id, organisation_id
            )
            if harness is None:
                # the harness was deleted (its bindings cascade), or is no longer visible — skip.
                continue
            out.append(
                BoundAgent(
                    harness_id=harness.id,
                    name=descriptor_name(harness.descriptor),
                    kind=DescriptorKind(harness.kind),
                    summary=descriptor_summary(harness.descriptor),
                )
            )
        return out

    async def list_by_harness(
        self, *, organisation_id: uuid.UUID, user_id: uuid.UUID, harness_id: uuid.UUID
    ) -> list[BoundGraph]:
        """The workspaces a harness serves — filtered to LIVE graphs (a dangling ``graph_id`` whose
        graph was deleted in KGS is skipped, ADR-029 §4) and named from the KGS membership set."""
        accessible = await self._graphs.accessible_graphs(
            organisation_id=organisation_id, user_id=user_id
        )
        out: list[BoundGraph] = []
        for binding in await self._bindings.list_by_harness(
            organisation_id=organisation_id, harness_capability_id=harness_id
        ):
            name = accessible.get(binding.graph_id)
            if name is None:
                continue  # dangling / not-visible graph — lazily ignored.
            out.append(BoundGraph(graph_id=binding.graph_id, name=name))
        return out
