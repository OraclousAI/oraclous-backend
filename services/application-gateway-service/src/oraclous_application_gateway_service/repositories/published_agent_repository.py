"""Published-agent store (ORAA-4 §21 repositories layer) — gateway-owned, org-scoped (ADR-019).

Every read/write filters ``organisation_id`` (ADR-006) — the only exception is the invoke-time
resolution by ``(org, slug)``, where the org comes from the integration key, never the request body.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_application_gateway_service.models.published_agent import PublishedAgent


class PublishedAgentRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        slug: str,
        bound_capability_ref: str,
        display_name: str | None = None,
        description: str | None = None,
    ) -> PublishedAgent:
        row = PublishedAgent(
            organisation_id=organisation_id,
            slug=slug,
            bound_capability_ref=bound_capability_ref,
            display_name=display_name,
            description=description,
        )
        async with self._session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def list_for_org(self, organisation_id: uuid.UUID) -> list[PublishedAgent]:
        async with self._session() as session:
            result = await session.execute(
                select(PublishedAgent)
                .where(PublishedAgent.organisation_id == organisation_id)
                .order_by(PublishedAgent.created_at.desc())
            )
            return list(result.scalars().all())

    async def get_by_slug(self, *, organisation_id: uuid.UUID, slug: str) -> PublishedAgent | None:
        """Resolve a published agent within an org (org from the caller)."""
        async with self._session() as session:
            result = await session.execute(
                select(PublishedAgent).where(
                    PublishedAgent.organisation_id == organisation_id,
                    PublishedAgent.slug == slug,
                )
            )
            return result.scalar_one_or_none()
