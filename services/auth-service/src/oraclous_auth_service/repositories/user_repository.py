"""User repository (ORAA-4 §21 repositories layer — the only ``users`` SQL).

Identity lookups (by email at login, by id) are deliberately **global / pre-auth**: they precede any
organisation context (you must identify the user before you can resolve their org). Email is
normalised to lowercase on every read and write so the unique index is the single source of identity
truth. This repo never touches another table.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_auth_service.models.user_model import User


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserRepository:
    """CRUD over ``users``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(
            select(User).where(User.email == normalize_email(email))
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: str) -> User | None:
        return await self._session.get(User, user_id)

    async def list_by_ids(self, ids: list[str]) -> list[User]:
        """Batch-resolve users by id (e.g. to attach emails to an org member roster)."""
        if not ids:
            return []
        result = await self._session.execute(select(User).where(User.id.in_(ids)))
        return list(result.scalars().all())

    async def create_user(
        self,
        *,
        id: str | None = None,
        email: str,
        password_hash: str | None,
        default_organisation_id: str,
        first_name: str | None = None,
        last_name: str | None = None,
        is_email_verified: bool = False,
    ) -> User:
        user = User(
            id=id or str(uuid.uuid4()),
            email=normalize_email(email),
            password_hash=password_hash,
            default_organisation_id=default_organisation_id,
            first_name=first_name,
            last_name=last_name,
            is_email_verified=is_email_verified,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def set_password(self, user_id: str, password_hash: str) -> User | None:
        user = await self.get_by_id(user_id)
        if user is not None:
            user.password_hash = password_hash
            await self._session.flush()
        return user

    async def set_email_verified(self, user_id: str) -> User | None:
        user = await self.get_by_id(user_id)
        if user is not None:
            user.is_email_verified = True
            await self._session.flush()
        return user
