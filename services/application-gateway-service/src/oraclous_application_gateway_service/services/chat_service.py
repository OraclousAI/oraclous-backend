"""Chat thread management (ORAA-4 §21 services layer) — start/list/transcript/soft-delete.

Org + creating-member scoped (a thread is private to its creator). A thread binds to a
published-agent slug, resolved via the S4 PublishedAgentRepository (one resolution path for chat +
invoke).
"""

from __future__ import annotations

import uuid

from oraclous_application_gateway_service.domain.pagination import DEFAULT_LIMIT
from oraclous_application_gateway_service.models.chat import ChatMessage, ChatThread
from oraclous_application_gateway_service.repositories.chat_repository import ChatRepository
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)


class UnknownAgent(Exception):
    """No active published agent at this slug in the org (-> 404)."""


class ChatService:
    def __init__(self, *, threads: ChatRepository, agents: PublishedAgentRepository) -> None:
        self._threads = threads
        self._agents = agents

    async def start_thread(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        agent_slug: str,
        title: str | None = None,
    ) -> ChatThread:
        agent = await self._agents.get_by_slug(organisation_id=organisation_id, slug=agent_slug)
        if agent is None or agent.status != "active":
            raise UnknownAgent(agent_slug)
        return await self._threads.create_thread(
            organisation_id=organisation_id,
            user_id=user_id,
            bound_agent_slug=agent_slug,
            title=title or "New chat",
        )

    async def list_threads(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[ChatThread]:
        return await self._threads.list_threads(
            organisation_id=organisation_id, user_id=user_id, limit=limit, offset=offset
        )

    async def get_messages(
        self,
        *,
        thread_id: uuid.UUID,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[ChatMessage] | None:
        thread = await self._threads.get_thread(
            thread_id=thread_id, organisation_id=organisation_id, user_id=user_id
        )
        if thread is None:
            return None
        return await self._threads.list_messages(thread_id=thread_id, limit=limit, offset=offset)

    async def delete_thread(
        self, *, thread_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        return await self._threads.soft_delete_thread(
            thread_id=thread_id, organisation_id=organisation_id, user_id=user_id
        )

    async def set_feedback(
        self,
        *,
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        rating: str,
    ) -> ChatMessage | None:
        """Rate an assistant message thumbs up/down (idempotent). Resolves the thread first (org +
        creating-member scoped) so a non-owner / cross-tenant request can't rate; returns None when
        the thread or the (assistant) message is not reachable for this member (-> 404)."""
        thread = await self._threads.get_thread(
            thread_id=thread_id, organisation_id=organisation_id, user_id=user_id
        )
        if thread is None:
            return None
        return await self._threads.set_message_rating(
            thread_id=thread_id, message_id=message_id, rating=rating
        )
