"""Published-agent management (ORAA-4 §21 services layer) — publish + list, org-scoped."""

from __future__ import annotations

import uuid

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

    async def list_agents(self, organisation_id: uuid.UUID) -> list[PublishedAgent]:
        return await self._repo.list_for_org(organisation_id)
