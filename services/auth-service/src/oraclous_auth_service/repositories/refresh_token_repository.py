"""Refresh-token repository (repositories layer — the only ``refresh_tokens`` SQL).

Backs the rotation family (threat T-REFRESH). Lookup is by ``jti`` — a 256-bit unguessable value
carried in the refresh JWT — so the read is global by design (like the credential-prefix lookup);
the row still carries ``organisation_id`` per ADR-006. ``rotate`` flips a presented token to
``rotated`` and ``revoke_family`` kills an entire chain when a rotated/revoked token is replayed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_auth_service.models.refresh_token_model import RefreshToken


class RefreshTokenRepository:
    """CRUD + rotation over ``refresh_tokens``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        jti: str,
        family_id: str,
        user_id: str,
        organisation_id: str,
        expires_at: datetime,
    ) -> RefreshToken:
        row = RefreshToken(
            id=str(uuid.uuid4()),
            organisation_id=organisation_id,
            user_id=user_id,
            jti=jti,
            family_id=family_id,
            status="active",
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_jti(self, jti: str) -> RefreshToken | None:
        result = await self._session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        return result.scalar_one_or_none()

    async def mark_rotated(self, jti: str, *, rotated_at: datetime) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti)
            .values(status="rotated", rotated_at=rotated_at)
        )
        await self._session.flush()

    async def revoke_family(self, family_id: str) -> int:
        """Revoke every still-active token in a family; return how many (reuse case).

        Commits immediately: reuse detection raises 401 right after this call, and the request
        session would otherwise roll the revocation back — but a detected stolen token MUST stay
        revoked regardless of the response. Committing here makes the family-kill durable.
        """
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.status != "revoked")
            .values(status="revoked")
        )
        await self._session.commit()
        return int(cast("CursorResult[object]", result).rowcount or 0)
