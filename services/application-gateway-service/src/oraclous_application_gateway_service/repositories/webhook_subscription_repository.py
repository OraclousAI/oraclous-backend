"""Webhook-subscription store (ORAA-4 §21 repositories layer) — a gateway-owned DB seam.

``get_by_id`` is intentionally NOT org-scoped: the opaque id IS the bearer-less credential the
inbound POST presents, and the row carries the org it then asserts to the engine. The MEMBER-facing
reads/writes (``list_for_org`` / ``delete_for_org``) are org-scoped (ADR-006), with the Postgres RLS
backstop (ADR-030) behind that app-layer filter.

The RLS backstop forces a TWO-ENGINE split (ADR-030 §3). ``get_by_id`` is a pre-auth producer that
resolves an inbound webhook's anchor BEFORE any org context, so it runs on the OWNER engine
(``install_guard=False``) which bypasses RLS — else FORCE'd RLS fails it closed to zero rows and
breaks inbound webhooks (the HARD RULE). Every ORG-BOUND method runs on the org-bound
``oraclous_app`` engine (the org-GUC guard installed by ``build_rls_engine``) and binds the org from
authenticated context via ``org_scope`` so the begin-guard sets ``app.current_organisation_id`` —
without that bind a read returns zero rows and a write raises 42501 (the capability-registry/engine
lesson).
"""

from __future__ import annotations

import uuid
from typing import cast

from oraclous_substrate import build_rls_engine, org_scope
from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_application_gateway_service.domain.pagination import DEFAULT_LIMIT
from oraclous_application_gateway_service.models.webhook_subscription import WebhookSubscription


class WebhookSubscriptionRepository:
    def __init__(self, db_url: str, *, install_guard: bool = True) -> None:
        # install_guard=True (default): the org-bound oraclous_app engine with the org-GUC begin
        # guard, so RLS bites + org_scope binds the GUC per org-bound op. install_guard=False: the
        # OWNER engine for the pre-auth ``get_by_id`` producer read (the inbound webhook's id is the
        # credential; it precedes org context and the owner bypasses RLS, so no guard — mirrors
        # auth's owner-engine credential store).
        self._engine = (
            build_rls_engine(db_url, echo=False)
            if install_guard
            else create_async_engine(db_url, echo=False)
        )
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
        """Org-bound: ``org_scope`` binds the GUC so the RLS WITH CHECK admits this INSERT."""
        row = WebhookSubscription(
            organisation_id=organisation_id,
            target_slug=target_slug,
            broker_secret_ref=broker_secret_ref,
            signature_scheme=signature_scheme,
            enabled=True,
        )
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
                await session.refresh(row)
                return row

    async def get_by_id(self, subscription_id: uuid.UUID) -> WebhookSubscription | None:
        """Resolve the inbound webhook's anchor by id alone (the id is the credential). Runs on the
        OWNER engine (no bound org — it precedes org context); the owner bypasses RLS so this
        resolves cross-org (ADR-030 §3)."""
        async with self._session() as session:
            result = await session.execute(
                select(WebhookSubscription).where(WebhookSubscription.id == subscription_id)
            )
            return result.scalar_one_or_none()

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> list[WebhookSubscription]:
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(WebhookSubscription)
                    .where(WebhookSubscription.organisation_id == organisation_id)
                    # stable ORDER BY (created_at desc, id desc) for a deterministic page (WP-10)
                    .order_by(WebhookSubscription.created_at.desc(), WebhookSubscription.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
                return list(result.scalars().all())

    async def delete_for_org(
        self, *, subscription_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> bool:
        """Org-scoped hard delete (the member removes their own sub). True if a row went."""
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        delete(WebhookSubscription).where(
                            WebhookSubscription.id == subscription_id,
                            WebhookSubscription.organisation_id == organisation_id,
                        )
                    )
                return cast("CursorResult[object]", result).rowcount > 0
