"""Published-agent store (ORAA-4 §21 repositories layer) — gateway-owned, org-scoped (ADR-019).

Every read/write filters ``organisation_id`` (ADR-006) — the only exception is the invoke-time
resolution by ``(org, slug)``, where the org comes from the integration key, never the request body.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_application_gateway_service.domain.pagination import DEFAULT_LIMIT
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

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> list[PublishedAgent]:
        async with self._session() as session:
            result = await session.execute(
                select(PublishedAgent)
                .where(PublishedAgent.organisation_id == organisation_id)
                # stable ORDER BY (created_at desc, id desc) for a deterministic page (WP-10)
                .order_by(PublishedAgent.created_at.desc(), PublishedAgent.id.desc())
                .limit(limit)
                .offset(offset)
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

    async def unpublish(self, *, organisation_id: uuid.UUID, slug: str) -> PublishedAgent | None:
        """Soft tombstone — status -> unpublished (mirrors the integration-key revoke). Org-scoped.
        Idempotent: an already-unpublished row is returned unchanged; absent slug returns None."""
        async with self._session() as session, session.begin():
            result = await session.execute(
                select(PublishedAgent).where(
                    PublishedAgent.organisation_id == organisation_id,
                    PublishedAgent.slug == slug,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.status = "unpublished"
            session.add(row)
        return row
