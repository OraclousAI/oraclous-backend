"""Organisation repository (repositories layer — the only ``organisations`` SQL).

Lookups are by id/slug; authorization (is the caller a member?) is enforced by the service via the
membership repository, not here — this is a plain data accessor. The slug unique index is the
race backstop; the service resolves a free slug via :meth:`slug_exists` before :meth:`create`, and
:meth:`create` reports a rejected slug in a form the service can retry (#676).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_auth_service.models.organisation_model import Organisation


class OrganisationRepository:
    """CRUD over ``organisations``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, id: str | None = None, name: str, slug: str, owner_user_id: str
    ) -> Organisation:
        """Insert an organisation, raising ``ConstraintViolation`` (``core/db_errors``) if the slug
        unique index rejects it — with the session still usable, so the caller can pick another
        candidate and try again (#676).

        The insert runs inside a SAVEPOINT for exactly that reason. ``core/database`` yields one
        session per request, so a bare failed ``flush`` poisons it: every later statement raises
        ``PendingRollbackError`` until a full rollback discards the whole request. The savepoint
        scopes the failure to this one insert instead.
        """
        org = Organisation(
            id=id or str(uuid.uuid4()),
            name=name,
            slug=slug,
            owner_user_id=owner_user_id,
            settings={},
            status="active",
        )
        # Settle earlier pending work before the savepoint opens, so rolling back undoes this
        # insert and nothing else.
        await self._session.flush()
        async with self._session.begin_nested():
            self._session.add(org)
            await self._session.flush()
        return org

    async def get_by_id(self, org_id: str) -> Organisation | None:
        return await self._session.get(Organisation, org_id)

    async def get_many(self, org_ids: list[str]) -> list[Organisation]:
        if not org_ids:
            return []
        result = await self._session.execute(
            select(Organisation).where(Organisation.id.in_(org_ids))
        )
        return list(result.scalars().all())

    async def slug_exists(self, slug: str) -> bool:
        result = await self._session.execute(
            select(Organisation.id).where(Organisation.slug == slug)
        )
        return result.scalar_one_or_none() is not None

    async def update(self, org_id: str, **fields: object) -> Organisation | None:
        org = await self.get_by_id(org_id)
        if org is None:
            return None
        for key, value in fields.items():
            if value is not None and hasattr(org, key):
                setattr(org, key, value)
        await self._session.flush()
        return org
