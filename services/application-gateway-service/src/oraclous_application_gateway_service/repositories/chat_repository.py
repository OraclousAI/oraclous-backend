"""Chat store (ORAA-4 §21 repositories layer) — gateway-owned, org-scoped + per-member (ADR-019).

Every thread read/write filters ``organisation_id`` AND ``created_by_user_id`` (private
to its creator within the org); a cross-tenant miss returns None (-> 404). Messages
are reached only through an already-resolved thread, so they inherit that scoping.

Both ``chat_threads`` and ``chat_messages`` are CLEAN tables under the RLS backstop (ADR-030): every
op is always reached with a bound org, so the repo runs on the org-bound ``oraclous_app`` engine
(``build_rls_engine`` installs the org-GUC guard) and binds the org via ``org_scope`` so the
begin-guard sets ``app.current_organisation_id`` and RLS scopes the op. The message-only reads
(``list_messages`` / ``recent_messages`` / ``set_message_rating``) take ``organisation_id``
explicitly — even though the caller already resolved the thread org-scoped, RLS on ``chat_messages``
needs the org bound on THIS transaction too (a missing bind reads zero rows + writes 42501 — the
capability-registry/engine lesson). The org is the one the caller resolved the thread under (the
authenticated principal's org), never request input (T1-M1).
"""

from __future__ import annotations

import uuid
from typing import cast

from oraclous_substrate import build_rls_engine, org_scope
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_application_gateway_service.domain.pagination import DEFAULT_LIMIT
from oraclous_application_gateway_service.models.chat import ChatMessage, ChatThread


class ChatRepository:
    def __init__(self, db_url: str, *, install_guard: bool = True) -> None:
        # install_guard=True (default): the org-bound oraclous_app engine with the org-GUC begin
        # guard. chat_threads + chat_messages are CLEAN tables (no pre-auth producer), so there is
        # no owner-engine path — install_guard is accepted only for symmetry with the
        # producer-bearing repos.
        self._engine = (
            build_rls_engine(db_url, echo=False)
            if install_guard
            else create_async_engine(db_url, echo=False)
        )
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    # --- threads (org + creating-member scoped); each binds the org via org_scope ---

    async def create_thread(
        self, *, organisation_id: uuid.UUID, user_id: uuid.UUID, bound_agent_slug: str, title: str
    ) -> ChatThread:
        row = ChatThread(
            organisation_id=organisation_id,
            created_by_user_id=user_id,
            bound_agent_slug=bound_agent_slug,
            title=title,
        )
        with org_scope(organisation_id):
            async with self._session() as session:
                session.add(row)
                await session.commit()
                await session.refresh(row)
                return row

    async def get_thread(
        self, *, thread_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID
    ) -> ChatThread | None:
        with org_scope(organisation_id):
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
        with org_scope(organisation_id):
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

        with org_scope(organisation_id):
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
                return cast("CursorResult[object]", result).rowcount > 0

    # --- messages (reached only via an already-resolved thread). They carry organisation_id so the
    # org-bound engine's GUC guard scopes the chat_messages op too (the thread resolution bound the
    # org on its OWN transaction; this op needs it bound on its own). ---

    async def list_messages(
        self,
        *,
        thread_id: uuid.UUID,
        organisation_id: uuid.UUID,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[ChatMessage]:
        with org_scope(organisation_id):
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

    async def recent_messages(
        self, *, thread_id: uuid.UUID, organisation_id: uuid.UUID, limit: int
    ) -> list[ChatMessage]:
        """The most-recent ``limit`` messages on a thread, returned oldest->newest. Used to build a
        chat turn's bounded context (the prior public ``list_messages`` returned the WHOLE
        transcript, then the caller kept the tail — this bounds the read while preserving exactly
        that 'most-recent window' behaviour, never the oldest page)."""
        with org_scope(organisation_id):
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
        self,
        *,
        thread_id: uuid.UUID,
        organisation_id: uuid.UUID,
        message_id: uuid.UUID,
        rating: str,
    ) -> ChatMessage | None:
        """Idempotently set a message's ``rating`` (a re-rate overwrites). Scoped to the
        already-resolved thread; only an ``assistant`` turn is ratable. Returns the updated row,
        or None if the message is absent / not on this thread / not an assistant turn (-> 404)."""
        with org_scope(organisation_id):
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
        message) in one transaction. Org-bound: the INSERT (chat_messages) AND the thread UPDATE
        (chat_threads) both bite the policy, so org_scope binds the GUC for both."""
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
        with org_scope(organisation_id):
            async with self._session() as session, session.begin():
                session.add(row)
                values: dict = {"last_message_at": func.now()}
                if new_title is not None:
                    values["title"] = new_title
                await session.execute(
                    update(ChatThread).where(ChatThread.id == thread_id).values(**values)
                )
            return row
