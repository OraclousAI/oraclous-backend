"""Integration: the ``delivery_state`` repo vs real Postgres (#515, E6 / O7).

The persisted half of the clean-delta: the last-written per-file content hashes for an
``(organisation_id, repo, ref)`` so a recurring refresh computes the diff (not a clobber), plus the
whole-delivery ``delivery_key`` under ``UNIQUE(organisation_id, delivery_key)`` so an identical
re-deliver dedupes to a NO_OP (the engine_jobs idempotency shape). Org-scoped: one org never reads
another's delivery state.

RED until #515 [impl] adds the ``delivery_state`` model + ``DeliveryStateRepository`` (the seam is
imported function-locally so collection stays green).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

pytestmark = pytest.mark.integration

_ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000005b2")


@pytest.fixture
async def repo(postgres_dsn: str) -> AsyncIterator:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    from oraclous_capability_registry_service.models import Base
    from oraclous_capability_registry_service.repositories.delivery_state_repository import (
        DeliveryStateRepository,
    )
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)  # creates delivery_state once the model lands
    await setup_engine.dispose()
    yield DeliveryStateRepository(async_dsn)


async def test_records_and_reads_back_the_per_file_hashes_org_scoped(repo) -> None:
    await repo.record(
        organisation_id=_ORG_A, repo="o/r", ref="deliver", file_hashes={"a.md": "h1", "b.md": "h2"}
    )
    assert await repo.get_hashes(organisation_id=_ORG_A, repo="o/r", ref="deliver") == {
        "a.md": "h1",
        "b.md": "h2",
    }
    # org-scoped: a different org never sees org A's delivery state (no cross-tenant decisions)
    assert await repo.get_hashes(organisation_id=_ORG_B, repo="o/r", ref="deliver") == {}


async def test_a_re_record_updates_the_changed_file_only(repo) -> None:
    await repo.record(
        organisation_id=_ORG_A, repo="o/r", ref="deliver", file_hashes={"a.md": "h1", "b.md": "h2"}
    )
    await repo.record(
        organisation_id=_ORG_A, repo="o/r", ref="deliver", file_hashes={"a.md": "h1", "b.md": "NEW"}
    )
    assert await repo.get_hashes(organisation_id=_ORG_A, repo="o/r", ref="deliver") == {
        "a.md": "h1",
        "b.md": "NEW",
    }


async def test_an_identical_redeliver_dedupes_via_the_delivery_key(repo) -> None:
    """The NO_OP guard: the same (org, delivery_key) twice collides on the UNIQUE constraint →
    record returns False the second time (an idempotent re-deliver writes nothing)."""
    fh = {"a.md": "h1", "b.md": "h2"}
    first = await repo.record(
        organisation_id=_ORG_A, repo="o/r", ref="deliver", file_hashes=fh, delivery_key="k1"
    )
    second = await repo.record(
        organisation_id=_ORG_A, repo="o/r", ref="deliver", file_hashes=fh, delivery_key="k1"
    )
    assert first is True and second is False  # second is the deduped NO_OP
    # a DIFFERENT org may reuse the same key value (the unique scope is per-org)
    assert (
        await repo.record(
            organisation_id=_ORG_B, repo="o/r", ref="deliver", file_hashes=fh, delivery_key="k1"
        )
        is True
    )
