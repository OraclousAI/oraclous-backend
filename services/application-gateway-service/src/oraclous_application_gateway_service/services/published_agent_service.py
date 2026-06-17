"""Published-agent management (ORAA-4 §21 services layer) — publish + list, org-scoped."""

from __future__ import annotations

import uuid

from oraclous_application_gateway_service.domain.pagination import DEFAULT_LIMIT
from oraclous_application_gateway_service.models.published_agent import PublishedAgent
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)


class PublishedAgentConflict(Exception):
    """A published agent with this slug already exists in the org (-> 409)."""


class PublishedAgentService:
    def __init__(self, repository: PublishedAgentRepository) -> None:
        self._repo = repository

    async def publish(
        self,
        *,
        organisation_id: uuid.UUID,
        slug: str,
        bound_capability_ref: str,
        display_name: str | None = None,
        description: str | None = None,
    ) -> PublishedAgent:
        if await self._repo.get_by_slug(organisation_id=organisation_id, slug=slug) is not None:
            raise PublishedAgentConflict(slug)
        return await self._repo.create(
            organisation_id=organisation_id,
            slug=slug,
            bound_capability_ref=bound_capability_ref,
            display_name=display_name,
            description=description,
        )

    async def list_agents(
        self, organisation_id: uuid.UUID, *, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> list[PublishedAgent]:
        return await self._repo.list_for_org(organisation_id, limit=limit, offset=offset)

    async def unpublish(self, *, organisation_id: uuid.UUID, slug: str) -> PublishedAgent | None:
        """Take a published agent down (status -> unpublished). Org-scoped, idempotent: returns the
        row (the existing read/invoke paths then 404 it), or None if no such slug in the org."""
        return await self._repo.unpublish(organisation_id=organisation_id, slug=slug)

    async def get_agent(self, *, organisation_id: uuid.UUID, slug: str) -> PublishedAgent | None:
        """Resolve a single published agent within the member's org (None -> 404 at the route)."""
        return await self._repo.get_by_slug(organisation_id=organisation_id, slug=slug)
