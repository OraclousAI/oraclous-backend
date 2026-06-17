"""Org-DEK store (ORAA-4 §21 repositories layer, ADR-020) — the only DB access for org_data_keys.

One wrapped DEK per org (UNIQUE ``organisation_id``). ``wrapped_dek`` is stored base64 (the
KEK-wrapped bytes); the plaintext DEK never touches this layer. ``create`` handles the unique-org
race when two requests lazily create the same org's first DEK — it re-reads + returns the winner.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_credential_broker_service.core.rls import build_rls_engine, org_scope
from oraclous_credential_broker_service.models.org_data_key import OrgDataKey


class OrgDataKeyRepository:
    def __init__(self, db_url: str) -> None:
        # ADR-030: engine carries the RLS org-GUC guard; org_scope binds the org per op.
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def get_for_org(self, *, organisation_id: uuid.UUID) -> OrgDataKey | None:
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(OrgDataKey).where(OrgDataKey.organisation_id == organisation_id)
                )
                return result.scalar_one_or_none()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        wrapped_dek: str,
        kek_provider: str,
        kek_key_id: str,
    ) -> OrgDataKey:
        """Insert this org's DEK wrap, returning the AUTHORITATIVE row. On the UNIQUE-org race (a
        concurrent first-write won) the insert raises and we re-read + return the winner — so the
        caller always unwraps the DEK that actually persisted (the loser's wrap is
        discarded)."""
        row = OrgDataKey(
            organisation_id=organisation_id,
            wrapped_dek=wrapped_dek,
            kek_provider=kek_provider,
            kek_key_id=kek_key_id,
        )
        try:
            with org_scope(organisation_id):
                async with self._session() as session:
                    async with session.begin():
                        session.add(row)
                    await session.refresh(row)
                    return row
        except IntegrityError:
            won = await self.get_for_org(organisation_id=organisation_id)
            if won is None:
                raise
            return won
