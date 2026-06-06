"""Org-membership repository (ORAA-4 §21 repositories layer — the only ``org_members`` SQL).

Backs the governance ``MembershipResolver`` (``organisations_for``) and the role checks that
authorise org management. Membership is the edge that scopes a human to an organisation.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_auth_service.models.organisation_model import OrgMember


class OrgMemberRepository:
    """CRUD over ``org_members``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, *, organisation_id: str, user_id: str, role: str) -> OrgMember:
        member = OrgMember(
            id=str(uuid.uuid4()),
            organisation_id=organisation_id,
            user_id=user_id,
            org_role=role,
        )
        self._session.add(member)
        await self._session.flush()
        return member

    async def get(self, *, organisation_id: str, user_id: str) -> OrgMember | None:
        result = await self._session.execute(
            select(OrgMember).where(
                OrgMember.organisation_id == organisation_id, OrgMember.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def role_for(self, *, organisation_id: str, user_id: str) -> str | None:
        member = await self.get(organisation_id=organisation_id, user_id=user_id)
        return member.org_role if member is not None else None

    async def organisation_ids_for(self, user_id: str) -> list[str]:
        result = await self._session.execute(
            select(OrgMember.organisation_id).where(OrgMember.user_id == user_id)
        )
        return [row for row in result.scalars().all()]

    async def list_members(self, organisation_id: str) -> list[OrgMember]:
        result = await self._session.execute(
            select(OrgMember)
            .where(OrgMember.organisation_id == organisation_id)
            .order_by(OrgMember.since, OrgMember.id)
        )
        return list(result.scalars().all())

    async def organisations_for(self, user_id: str) -> list[uuid.UUID]:
        """Governance ``MembershipResolver`` shape: the orgs a user belongs to, as UUIDs."""
        return [uuid.UUID(oid) for oid in await self.organisation_ids_for(user_id)]

    async def update_role(
        self, *, organisation_id: str, user_id: str, role: str
    ) -> OrgMember | None:
        member = await self.get(organisation_id=organisation_id, user_id=user_id)
        if member is not None:
            member.org_role = role
            await self._session.flush()
        return member

    async def remove(self, *, organisation_id: str, user_id: str) -> bool:
        member = await self.get(organisation_id=organisation_id, user_id=user_id)
        if member is None:
            return False
        await self._session.delete(member)
        await self._session.flush()
        return True
