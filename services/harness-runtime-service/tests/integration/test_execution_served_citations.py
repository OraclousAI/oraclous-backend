"""#784 (folded into #782) — the persisted served-citation set, vs real Postgres.

``served_citation_ids`` is what rule 2 checks an answer against, and #782 makes it load-bearing at
the run boundary. Until now the column had no test at any level: the only test that reaches
``update_run`` swaps in a fake repository, so the UNION on line 202 never runs, and a regression
there would surface as a correct answer being blocked after a HITL resume — the hardest possible
place to notice it.

The union is not a detail. The loop's own set covers the post-resume segment ONLY (the checkpoint
carries the transcript, not platform counters), so a replace would silently drop every citation
served before the pause and turn a legitimate post-pause answer into a rule 2 violation.

Key-free: a testcontainer Postgres; the repo self-binds the org (ADR-030 ``org_scope``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_CIT_A = "cit_9f2a4c81b7d3e50a1c6f28934bd5e7a0"
_CIT_B = "cit_0b1d3f57a9c2e4680d8f1a3b5c7e9021"
_CIT_C = "cit_4e6a8c02b1d3f5079a2c4e6081b3d5f7"


@pytest.fixture
async def repo(postgres_dsn: str) -> AsyncIterator[Any]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    from oraclous_harness_runtime_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup = create_async_engine(async_dsn)
    async with setup.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup.dispose()

    from oraclous_harness_runtime_service.repositories.execution_repository import (
        ExecutionRepository,
    )

    r = ExecutionRepository(async_dsn)
    yield r
    await r.close()


async def _create(repo: Any, *, served: list[str] | None) -> Any:
    return await repo.create(
        execution_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        harness_name="T",
        content_hash=None,
        status="ESCALATED",
        input_text="go",
        output="partial",
        error_type="hitl_required",
        error_message="waiting",
        iterations=1,
        total_tokens=1,
        steps=[],
        served_citation_ids=served,
    )


async def _update(repo: Any, row: Any, *, served: list[str] | None) -> Any:
    return await repo.update_run(
        row.id,
        _ORG,
        status="SUCCEEDED",
        output="done",
        error_type=None,
        error_message=None,
        iterations=3,
        total_tokens=9,
        steps=[],
        served_citation_ids=served,
    )


# --- create: the column is a list, never NULL ------------------------------------------------


async def test_a_run_that_served_nothing_records_an_empty_list_not_null(repo: Any) -> None:
    # The gate reads this on every run. A NULL would make "served nothing" indistinguishable from
    # "the writer forgot to record" at the one moment the difference decides whether an answer is
    # blocked — and a fail-open there is exactly the fabrication this Contract exists to stop.
    row = await _create(repo, served=None)
    assert row.served_citation_ids == []
    fetched = await repo.get(row.id, _ORG)
    assert fetched.served_citation_ids == []


async def test_the_created_set_round_trips_in_order(repo: Any) -> None:
    row = await _create(repo, served=[_CIT_A, _CIT_B])
    fetched = await repo.get(row.id, _ORG)
    assert list(fetched.served_citation_ids) == [_CIT_A, _CIT_B]


# --- update_run: a resume UNIONS, it never replaces -------------------------------------------


async def test_a_resume_unions_the_new_segment_into_the_persisted_set(repo: Any) -> None:
    # The pre-pause segment served A; the post-resume segment served B. Replacing would drop A, and
    # a post-pause answer citing A — a source the member really was handed — would fail rule 2.
    row = await _create(repo, served=[_CIT_A])
    updated = await _update(repo, row, served=[_CIT_B])
    assert set(updated.served_citation_ids) == {_CIT_A, _CIT_B}


async def test_the_union_preserves_first_seen_order(repo: Any) -> None:
    # Order is the order the platform served them, which is the order a console lists sources in.
    row = await _create(repo, served=[_CIT_A, _CIT_B])
    updated = await _update(repo, row, served=[_CIT_C])
    assert list(updated.served_citation_ids) == [_CIT_A, _CIT_B, _CIT_C]


async def test_re_serving_the_same_id_does_not_duplicate_it(repo: Any) -> None:
    # citation_id is deterministic, so a member that re-reads one document version across a pause
    # is handed the same id twice. The row records WHAT was served, not how many times.
    row = await _create(repo, served=[_CIT_A, _CIT_B])
    updated = await _update(repo, row, served=[_CIT_B, _CIT_C])
    assert list(updated.served_citation_ids) == [_CIT_A, _CIT_B, _CIT_C]


async def test_a_segment_that_served_nothing_leaves_the_set_intact(repo: Any) -> None:
    # A resume whose new segment retrieved nothing is the common case — the human approved a write,
    # the member finished. Clearing the set here would blank the evidence for the whole run.
    row = await _create(repo, served=[_CIT_A])
    updated = await _update(repo, row, served=[])
    assert list(updated.served_citation_ids) == [_CIT_A]


async def test_omitting_the_set_on_an_update_leaves_it_intact(repo: Any) -> None:
    # Every other caller of update_run (the DENIED path, a chained re-pause) passes nothing. None
    # must mean "I have no new information", never "clear it".
    row = await _create(repo, served=[_CIT_A, _CIT_B])
    updated = await _update(repo, row, served=None)
    assert list(updated.served_citation_ids) == [_CIT_A, _CIT_B]


async def test_the_union_survives_a_re_read(repo: Any) -> None:
    # The merge assigns a new list to a JSON column; an in-place mutation would not be flushed and
    # the row would look correct in memory while the database kept the old value.
    row = await _create(repo, served=[_CIT_A])
    await _update(repo, row, served=[_CIT_B])
    fetched = await repo.get(row.id, _ORG)
    assert set(fetched.served_citation_ids) == {_CIT_A, _CIT_B}
