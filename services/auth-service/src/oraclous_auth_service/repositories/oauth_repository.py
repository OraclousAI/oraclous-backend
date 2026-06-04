"""OAuth repositories (ORAA-4 §21 repositories layer — the only oauth_* SQL).

``OAuthAccountRepository`` upserts a user's encrypted provider tokens (org-scoped).
``OAuthStateRepository`` stores + atomically consumes single-use handshake state (replay-safe).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_auth_service.models.oauth_model import OAuthAccount, OAuthState


class OAuthAccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, *, organisation_id: str, user_id: str, provider: str
    ) -> OAuthAccount | None:
        result = await self._session.execute(
            select(OAuthAccount).where(
                OAuthAccount.organisation_id == organisation_id,
                OAuthAccount.user_id == user_id,
                OAuthAccount.provider == provider,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        organisation_id: str,
        user_id: str,
        provider: str,
        access_token_enc: str,
        refresh_token_enc: str | None,
        scopes: list[str] | None,
        expires_at: datetime | None,
    ) -> OAuthAccount:
        existing = await self.get(
            organisation_id=organisation_id, user_id=user_id, provider=provider
        )
        if existing is not None:
            existing.access_token_enc = access_token_enc
            if refresh_token_enc:
                existing.refresh_token_enc = refresh_token_enc
            existing.scopes = scopes
            existing.expires_at = expires_at
            await self._session.flush()
            return existing
        row = OAuthAccount(
            id=str(uuid.uuid4()),
            organisation_id=organisation_id,
            user_id=user_id,
            provider=provider,
            access_token_enc=access_token_enc,
            refresh_token_enc=refresh_token_enc,
            scopes=scopes,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row


class OAuthStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        state: str,
        provider: str,
        code_verifier_enc: str,
        redirect_uri: str,
        expires_at: datetime,
    ) -> None:
        self._session.add(
            OAuthState(
                state=state,
                provider=provider,
                code_verifier_enc=code_verifier_enc,
                redirect_uri=redirect_uri,
                expires_at=expires_at,
            )
        )
        await self._session.flush()

    async def consume(self, *, state: str, now: datetime) -> OAuthState | None:
        """Atomically mark a still-valid state consumed (single-use); return it, or None if not.

        The ``WHERE consumed = false`` predicate makes a replayed/used state resolve to no row, so a
        second callback with the same state is rejected (T-OAUTH state replay).
        """
        result = await self._session.execute(
            update(OAuthState)
            .where(
                OAuthState.state == state,
                OAuthState.consumed.is_(False),
                OAuthState.expires_at > now,
            )
            .values(consumed=True)
            .returning(OAuthState)
        )
        return result.scalar_one_or_none()
