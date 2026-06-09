"""Webhook-subscription store (ORAA-4 §21 repositories layer) — the only DB seam for it.

``get_by_id`` is intentionally NOT org-scoped: the opaque id IS the bearer-less credential the
inbound POST presents, and the row carries the org it then asserts to the engine. The MEMBER-facing
reads/writes (``list_for_org`` / ``delete_for_org``) are org-scoped (ADR-006).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_application_gateway_service.models.webhook_subscription import WebhookSubscription


class WebhookSubscriptionRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        target_slug: str,
        broker_secret_ref: uuid.UUID,
        signature_scheme: str = "generic",
    ) -> WebhookSubscription:
        row = WebhookSubscription(
            organisation_id=organisation_id,
            target_slug=target_slug,
            broker_secret_ref=broker_secret_ref,
            signature_scheme=signature_scheme,
            enabled=True,
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def get_by_id(self, subscription_id: uuid.UUID) -> WebhookSubscription | None:
        """Resolve the inbound webhook's anchor by id alone (the id is the credential)."""
        async with self._session() as session:
            result = await session.execute(
                select(WebhookSubscription).where(WebhookSubscription.id == subscription_id)
            )
            return result.scalar_one_or_none()

    async def list_for_org(self, organisation_id: uuid.UUID) -> list[WebhookSubscription]:
        async with self._session() as session:
            result = await session.execute(
                select(WebhookSubscription)
                .where(WebhookSubscription.organisation_id == organisation_id)
                .order_by(WebhookSubscription.created_at.desc())
            )
            return list(result.scalars().all())

    async def delete_for_org(
        self, *, subscription_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> bool:
        """Org-scoped hard delete (the member removes their own sub). True if a row went."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    delete(WebhookSubscription).where(
                        WebhookSubscription.id == subscription_id,
                        WebhookSubscription.organisation_id == organisation_id,
                    )
                )
            return result.rowcount > 0
