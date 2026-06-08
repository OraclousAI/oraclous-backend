"""Integration-key management (ORAA-4 §21 services layer) — mint/list/get/rotate/revoke, org-scoped.

Mint generates a key (the plaintext is returned once, never stored) bound to a published-agent slug
(validated to exist in the org) XOR a capability allow-list. Org-scoped — a member
manages only their own organisation's keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from oraclous_application_gateway_service.domain.integration_key import MintedKey, mint_key
from oraclous_application_gateway_service.models.integration_key import IntegrationKey
from oraclous_application_gateway_service.repositories.integration_key_repository import (
    IntegrationKeyRepository,
)
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)


class UnknownBoundAgent(Exception):
    """``bound_agent_slug`` does not name a published agent in this org (-> 404)."""


class IntegrationKeyManagementService:
    def __init__(self, *, keys: IntegrationKeyRepository, agents: PublishedAgentRepository) -> None:
        self._keys = keys
        self._agents = agents

    async def mint(
        self,
        *,
        organisation_id: uuid.UUID,
        bound_agent_slug: str | None = None,
        capability_allow_list: list[str] | None = None,
        cors_origins: list[str] | None = None,
        rate_limit: int | None = None,
        rate_window_seconds: int | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[MintedKey, IntegrationKey]:
        if bound_agent_slug is not None:
            agent = await self._agents.get_by_slug(
                organisation_id=organisation_id, slug=bound_agent_slug
            )
            if agent is None:
                raise UnknownBoundAgent(bound_agent_slug)
        minted = mint_key("oak")
        row = await self._keys.create(
            organisation_id=organisation_id,
            key_prefix=minted.key_prefix,
            key_hash=minted.key_hash,
            last4=minted.last4,
            bound_agent_slug=bound_agent_slug,
            capability_allow_list=capability_allow_list,
            cors_origins=cors_origins,
            rate_limit=rate_limit,
            rate_window_seconds=rate_window_seconds,
            expires_at=expires_at,
        )
        return minted, row

    async def list_keys(self, organisation_id: uuid.UUID) -> list[IntegrationKey]:
        return await self._keys.list_for_org(organisation_id)

    async def get(self, *, key_id: uuid.UUID, organisation_id: uuid.UUID) -> IntegrationKey | None:
        return await self._keys.get_for_org(key_id=key_id, organisation_id=organisation_id)

    async def rotate(
        self, *, key_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> tuple[MintedKey | None, IntegrationKey | None]:
        minted = mint_key("oak")
        row = await self._keys.rotate(
            key_id=key_id,
            organisation_id=organisation_id,
            key_prefix=minted.key_prefix,
            key_hash=minted.key_hash,
            last4=minted.last4,
        )
        return (minted, row) if row is not None else (None, None)

    async def revoke(
        self, *, key_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> IntegrationKey | None:
        return await self._keys.revoke(key_id=key_id, organisation_id=organisation_id)
