"""Invitation repository (repositories layer — the only ``org_invitations`` SQL).

Lookup by token_prefix is a deliberate pre-auth global resolve (the accepter presents only the raw
token); the row still carries ``organisation_id``. Listing/revoking is org-scoped by the caller.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_auth_service.models.invitation_model import OrgInvitation


class InvitationRepository:
    """CRUD over ``org_invitations``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        organisation_id: str,
        email: str,
        org_role: str,
        token_hash: str,
        token_prefix: str,
        invited_by_user_id: str,
        expires_at: datetime,
        subgraph_grants: dict | None = None,
    ) -> OrgInvitation:
        inv = OrgInvitation(
            id=str(uuid.uuid4()),
            organisation_id=organisation_id,
            email=email,
            org_role=org_role,
            token_hash=token_hash,
            token_prefix=token_prefix,
            status="pending",
            subgraph_grants=subgraph_grants,
            invited_by_user_id=invited_by_user_id,
            expires_at=expires_at,
        )
        self._session.add(inv)
        await self._session.flush()
        return inv

    async def by_prefix_pending(self, prefix: str) -> list[OrgInvitation]:
        result = await self._session.execute(
            select(OrgInvitation).where(
                OrgInvitation.token_prefix == prefix, OrgInvitation.status == "pending"
            )
        )
        return list(result.scalars().all())

    async def get(self, *, invitation_id: str, organisation_id: str) -> OrgInvitation | None:
        result = await self._session.execute(
            select(OrgInvitation).where(
                OrgInvitation.id == invitation_id,
                OrgInvitation.organisation_id == organisation_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_org(self, organisation_id: str) -> list[OrgInvitation]:
        result = await self._session.execute(
            select(OrgInvitation)
            .where(OrgInvitation.organisation_id == organisation_id)
            .order_by(OrgInvitation.created_at, OrgInvitation.id)
        )
        return list(result.scalars().all())

    async def supersede_pending(self, *, organisation_id: str, email: str) -> int:
        """Mark any existing pending invitation for (org, email) as revoked. Returns the count."""
        result = await self._session.execute(
            update(OrgInvitation)
            .where(
                OrgInvitation.organisation_id == organisation_id,
                OrgInvitation.email == email,
                OrgInvitation.status == "pending",
            )
            .values(status="revoked")
        )
        await self._session.flush()
        return int(cast("CursorResult[object]", result).rowcount or 0)

    async def mark_accepted(
        self, *, invitation_id: str, accepted_by_user_id: str, accepted_at: datetime
    ) -> None:
        await self._session.execute(
            update(OrgInvitation)
            .where(OrgInvitation.id == invitation_id)
            .values(
                status="accepted",
                accepted_by_user_id=accepted_by_user_id,
                accepted_at=accepted_at,
            )
        )
        await self._session.flush()

    async def revoke(self, *, invitation_id: str, organisation_id: str) -> bool:
        result = await self._session.execute(
            update(OrgInvitation)
            .where(
                OrgInvitation.id == invitation_id,
                OrgInvitation.organisation_id == organisation_id,
                OrgInvitation.status == "pending",
            )
            .values(status="revoked")
        )
        await self._session.flush()
        return (cast("CursorResult[object]", result).rowcount or 0) > 0
