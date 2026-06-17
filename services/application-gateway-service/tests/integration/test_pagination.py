"""Integration (WP-10): the gateway's collection repositories page vs a REAL Postgres.

Exercises the actual ``limit``/``offset`` + stable ``ORDER BY`` on the gateway-owned stores against
a real DB (testcontainers), the safety net for bounding these live-surface reads:
  - ``ChatRepository.list_threads`` / ``list_messages`` (member chat plane);
  - ``PublishedAgentRepository.list_for_org`` (member agents plane);
  - ``ChatRepository.recent_messages`` returns the MOST-RECENT window oldest->newest (the chat-turn
    context read — bounded, never the oldest page).
Defaults are backward-compatible (no params -> the whole small set, in stable order); a bounded page
is a contiguous slice of that same stable order, so paging is deterministic.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

pytestmark = pytest.mark.integration

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000000f1")


@pytest.fixture
async def repos(postgres_dsn: str) -> AsyncIterator[dict]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

    from oraclous_application_gateway_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from oraclous_application_gateway_service.repositories.chat_repository import ChatRepository
    from oraclous_application_gateway_service.repositories.published_agent_repository import (
        PublishedAgentRepository,
    )

    chat = ChatRepository(async_dsn)
    agents = PublishedAgentRepository(async_dsn)
    try:
        yield {"chat": chat, "agents": agents}
    finally:
        await chat.close()
        await agents.close()


async def test_list_threads_pages_in_stable_order(repos: dict) -> None:
    chat = repos["chat"]
    for i in range(5):
        await chat.create_thread(
            organisation_id=_ORG, user_id=_USER, bound_agent_slug="weather", title=f"t{i}"
        )

    everything = await chat.list_threads(organisation_id=_ORG, user_id=_USER)  # default window
    assert len(everything) == 5  # backward-compatible: no params -> the whole (small) set

    page1 = await chat.list_threads(organisation_id=_ORG, user_id=_USER, limit=2, offset=0)
    page2 = await chat.list_threads(organisation_id=_ORG, user_id=_USER, limit=2, offset=2)
    page3 = await chat.list_threads(organisation_id=_ORG, user_id=_USER, limit=2, offset=4)
    # the pages are disjoint, contiguous slices of the same stable order (deterministic)
    paged_ids = [t.id for t in (*page1, *page2, *page3)]
    assert paged_ids == [t.id for t in everything]
    assert len(page1) == 2 and len(page3) == 1


async def test_list_messages_pages_and_recent_window(repos: dict) -> None:
    chat = repos["chat"]
    thread = await chat.create_thread(
        organisation_id=_ORG, user_id=_USER, bound_agent_slug="weather", title="t"
    )
    for i in range(6):
        await chat.add_message(
            thread_id=thread.id, organisation_id=_ORG, role="user", content=f"m{i}"
        )

    full = await chat.list_messages(thread_id=thread.id)  # default, oldest->newest
    assert [m.content for m in full] == ["m0", "m1", "m2", "m3", "m4", "m5"]

    page = await chat.list_messages(thread_id=thread.id, limit=2, offset=2)
    assert [m.content for m in page] == ["m2", "m3"]

    # recent_messages: the MOST-RECENT window, oldest->newest (the bounded turn-context read)
    recent = await chat.recent_messages(thread_id=thread.id, limit=3)
    assert [m.content for m in recent] == ["m3", "m4", "m5"]


async def test_list_agents_pages_in_stable_order(repos: dict) -> None:
    agents = repos["agents"]
    for i in range(4):
        await agents.create(
            organisation_id=_ORG, slug=f"agent-{i}", bound_capability_ref=f"cap-{i}"
        )

    everything = await agents.list_for_org(_ORG)
    assert len(everything) == 4

    page = await agents.list_for_org(_ORG, limit=2, offset=1)
    assert [a.id for a in page] == [a.id for a in everything[1:3]]
