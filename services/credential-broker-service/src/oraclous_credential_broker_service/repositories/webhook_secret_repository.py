"""Webhook-secret store (ORAA-4 §21 repositories layer) — org-scoped, the only DB access for it.

Mirrors the ``CredentialRepository`` engine/session idiom. ``get_for_org`` filters
``organisation_id`` (ADR-006) — a cross-org id returns None (the route then 404s).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_credential_broker_service.models.webhook_secret import WebhookSecret


class WebhookSecretRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(self, *, organisation_id: uuid.UUID, encrypted_secret: str) -> WebhookSecret:
        row = WebhookSecret(organisation_id=organisation_id, encrypted_secret=encrypted_secret)
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def get_for_org(
        self, *, secret_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> WebhookSecret | None:
        async with self._session() as session:
            result = await session.execute(
                select(WebhookSecret).where(
                    WebhookSecret.id == secret_id,
                    WebhookSecret.organisation_id == organisation_id,
                )
            )
            return result.scalar_one_or_none()
