"""Chat store (ORAA-4 §21 repositories layer) — gateway-owned, org-scoped + per-member (ADR-019).

Every thread read/write filters ``organisation_id`` AND ``created_by_user_id`` (private
to its creator within the org); a cross-tenant miss returns None (-> 404). Messages
are reached only through an already-resolved thread, so they inherit that scoping.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_application_gateway_service.domain.pagination import DEFAULT_LIMIT
from oraclous_application_gateway_service.models.chat import ChatMessage, ChatThread


class ChatRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    # --- threads (org + creating-member scoped) ---

    async def create_thread(
        self, *, organisation_id: uuid.UUID, user_id: uuid.UUID, bound_agent_slug: str, title: str
    ) -> ChatThread:
        row = ChatThread(
            organisation_id=organisation_id,
            created_by_user_id=user_id,
            bound_agent_slug=bound_agent_slug,
            title=title,
        )
        async with self._session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def get_thread(
        self, *, thread_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID
    ) -> ChatThread | None:
        async with self._session() as session:
            result = await session.execute(
                select(ChatThread).where(
                    ChatThread.id == thread_id,
                    ChatThread.organisation_id == organisation_id,
                    ChatThread.created_by_user_id == user_id,
                    ChatThread.deleted_at.is_(None),
                )
            )
            return result.scalar_one_or_none()

    async def list_threads(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[ChatThread]:
        async with self._session() as session:
            result = await session.execute(
                select(ChatThread)
                .where(
                    ChatThread.organisation_id == organisation_id,
                    ChatThread.created_by_user_id == user_id,
                    ChatThread.deleted_at.is_(None),
                )
                # stable ORDER BY (last_message_at desc, id desc tiebreak) so the page window is
                # deterministic even when several threads share a last_message_at (WP-10).
                .order_by(ChatThread.last_message_at.desc(), ChatThread.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all())

    async def soft_delete_thread(
        self, *, thread_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        from sqlalchemy import func

        async with self._session() as session, session.begin():
            result = await session.execute(
                update(ChatThread)
                .where(
                    ChatThread.id == thread_id,
                    ChatThread.organisation_id == organisation_id,
                    ChatThread.created_by_user_id == user_id,
                    ChatThread.deleted_at.is_(None),
                )
                .values(deleted_at=func.now())
            )
            return result.rowcount > 0

    # --- messages (reached only via an already-resolved thread) ---

    async def list_messages(
        self, *, thread_id: uuid.UUID, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> list[ChatMessage]:
        async with self._session() as session:
            result = await session.execute(
                select(ChatMessage)
                .where(ChatMessage.thread_id == thread_id)
                # stable ORDER BY (created_at, id tiebreak) so the page window is deterministic
                # even when two messages share a created_at (WP-10).
                .order_by(ChatMessage.created_at, ChatMessage.id)
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all())

    async def recent_messages(self, *, thread_id: uuid.UUID, limit: int) -> list[ChatMessage]:
        """The most-recent ``limit`` messages on a thread, returned oldest->newest. Used to build a
        chat turn's bounded context (the prior public ``list_messages`` returned the WHOLE
        transcript, then the caller kept the tail — this bounds the read while preserving exactly
        that 'most-recent window' behaviour, never the oldest page)."""
        async with self._session() as session:
            result = await session.execute(
                select(ChatMessage)
                .where(ChatMessage.thread_id == thread_id)
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(limit)
            )
            rows = list(result.scalars().all())
        rows.reverse()  # back to oldest->newest, the order build_turn_input expects
        return rows

    async def set_message_rating(
        self, *, thread_id: uuid.UUID, message_id: uuid.UUID, rating: str
    ) -> ChatMessage | None:
        """Idempotently set a message's ``rating`` (a re-rate overwrites). Scoped to the
        already-resolved thread; only an ``assistant`` turn is ratable. Returns the updated row,
        or None if the message is absent / not on this thread / not an assistant turn (-> 404)."""
        async with self._session() as session, session.begin():
            result = await session.execute(
                update(ChatMessage)
                .where(
                    ChatMessage.id == message_id,
                    ChatMessage.thread_id == thread_id,
                    ChatMessage.role == "assistant",
                )
                .values(rating=rating)
                .returning(ChatMessage)
            )
            return result.scalar_one_or_none()

    async def add_message(
        self,
        *,
        thread_id: uuid.UUID,
        organisation_id: uuid.UUID,
        role: str,
        content: str,
        execution_id: uuid.UUID | None = None,
        total_tokens: int | None = None,
        sources: list | None = None,
        new_title: str | None = None,
    ) -> ChatMessage:
        """Append a message + touch the thread's ``last_message_at`` (and ``title`` on the first
        message) in one transaction."""
        from sqlalchemy import func

        row = ChatMessage(
            thread_id=thread_id,
            organisation_id=organisation_id,
            role=role,
            content=content,
            execution_id=execution_id,
            total_tokens=total_tokens,
            sources=sources,
        )
        async with self._session() as session, session.begin():
            session.add(row)
            values: dict = {"last_message_at": func.now()}
            if new_title is not None:
                values["title"] = new_title
            await session.execute(
                update(ChatThread).where(ChatThread.id == thread_id).values(**values)
            )
        return row
