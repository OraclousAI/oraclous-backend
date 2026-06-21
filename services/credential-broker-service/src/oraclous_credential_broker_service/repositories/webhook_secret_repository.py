"""Webhook-secret store (repositories layer) — org-scoped, the only DB access for it.

Mirrors the ``CredentialRepository`` engine/session idiom. ``get_for_org`` filters
``organisation_id`` (ADR-006) — a cross-org id returns None (the route then 404s).
"""

from __future__ import annotations

import uuid
from typing import cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_credential_broker_service.core.rls import build_rls_engine, org_scope
from oraclous_credential_broker_service.models.webhook_secret import WebhookSecret


class WebhookSecretRepository:
    def __init__(self, db_url: str) -> None:
        # ADR-030: engine carries the RLS org-GUC guard; org_scope binds the org per op.
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(self, *, organisation_id: uuid.UUID, encrypted_secret: str) -> WebhookSecret:
        row = WebhookSecret(organisation_id=organisation_id, encrypted_secret=encrypted_secret)
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
                await session.refresh(row)
                return row

    async def get_for_org(
        self, *, secret_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> WebhookSecret | None:
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(WebhookSecret).where(
                        WebhookSecret.id == secret_id,
                        WebhookSecret.organisation_id == organisation_id,
                    )
                )
                return result.scalar_one_or_none()

    async def delete_for_org(self, *, secret_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        """Hard-delete a secret (org-scoped). Returns True if a row was removed. Used by the GW's
        webhook orphan-secret GC (R7-SEC S4) — a cross-org id matches nothing (ADR-006)."""
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        delete(WebhookSecret).where(
                            WebhookSecret.id == secret_id,
                            WebhookSecret.organisation_id == organisation_id,
                        )
                    )
                return (cast("CursorResult[object]", result).rowcount or 0) > 0

    async def iter_all_ciphertexts(self) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
        """Every secret as ``(id, organisation_id, encrypted_secret)`` — the backfill sweep."""
        async with self._session() as session:
            result = await session.execute(
                select(
                    WebhookSecret.id,
                    WebhookSecret.organisation_id,
                    WebhookSecret.encrypted_secret,
                )
            )
            return [(r[0], r[1], r[2]) for r in result.all()]

    async def set_encrypted_secret(self, *, secret_id: uuid.UUID, encrypted_secret: str) -> None:
        """Overwrite a row's ciphertext in place (the backfill, after a v1→v2 re-encrypt)."""
        async with self._session() as session, session.begin():
            obj = await session.get(WebhookSecret, secret_id)
            if obj is not None:
                obj.encrypted_secret = encrypted_secret
