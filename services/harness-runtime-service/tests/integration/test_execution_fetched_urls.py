"""#975 (§CITE cite-by-reference) — the persisted fetch-registry column, vs real Postgres.

Template: ``test_execution_served_citations.py`` (#743/#782), copied exactly (plan §4: "the 0008
shape"). ``fetched_urls`` is the run's own registry of URLs the platform actually fetched — the
mechanism cite-by-reference numbers ``[Sn]`` markers against. Its persistence carries the SAME two
properties ``served_citation_ids`` does, for the SAME reason:

* **NOT NULL, default ``'[]'::jsonb``** — the acceptance pass reads it on every run, and a NULL
  would make "fetched nothing" indistinguishable from "not recorded".
* **``update_run`` UNIONS, it never replaces** — the loop's own accumulator covers the post-resume
  segment only (the checkpoint carries the transcript, not platform counters), so a replace would
  silently drop every URL fetched before a HITL pause, and a post-pause marker citing one of them
  would resolve to nothing.

One property is NEW here and has no ``served_citation_ids`` analogue: the union is CAPPED at
``_MAX_FETCHED_URLS`` (2000, already shipped in ``domain/loop/tool_use.py`` for the loop's own
accumulator — imported, never hand-derived) and truncates the TAIL, keeping the head (and therefore
every already-numbered ``[Sn]`` marker) stable across a resume.

Key-free: a testcontainer Postgres; the repo self-binds the org (ADR-030 ``org_scope``).

Every name new to this slice (the migration revision id, ``fetched_urls`` on ``create``/
``update_run``) is imported/referenced function-locally per
``.claude/rules/tests-seam-imports.md``; the assertions fail RED until the ``[impl]`` lands
(``create``/``update_run`` don't accept the kwarg yet, and migration ``0009_fetched_urls`` doesn't
exist).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000009fe")
_A = "https://example.com/a"
_B = "https://example.com/b"
_C = "https://example.com/c"


# --- migration 0009 itself, upgrade head / downgrade -1, vs real Postgres ----------------------


def _reset_schema(dsn: str) -> None:
    """Full reset: alembic's own version table persists across runs on the SHARED session
    container, and a table left behind by a sibling fixture's raw ``Base.metadata.create_all``
    (no alembic bookkeeping) would make ``command.upgrade`` fail with "relation already exists"."""
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")


def _columns(dsn: str, table: str) -> dict[str, dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, is_nullable, data_type, column_default "
            "FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        return {
            row[0]: {"nullable": row[1] == "YES", "type": row[2], "default": row[3]}
            for row in cur.fetchall()
        }


def _alembic_config() -> Any:
    """Built as a plain (sync) helper, never inline in the async test, so the ``Path``/``Config``
    filesystem calls never run directly inside an ``async def`` body (ASYNC240)."""
    from alembic.config import Config

    service_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(service_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(service_root / "migrations"))
    return cfg


async def test_migration_0009_adds_fetched_urls_jsonb_not_null_default_empty_round_trip(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic import command
    from oraclous_harness_runtime_service.core.config import get_settings

    _reset_schema(postgres_dsn)
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    # env.py reads the DSN from Settings, not from the Config object we build below — the running
    # process's env is the only lever that actually reaches it (see migrations/env.py).
    monkeypatch.setenv("HARNESS_DATABASE_URL", async_dsn)
    get_settings.cache_clear()
    try:
        cfg = _alembic_config()

        command.upgrade(cfg, "head")
        cols = _columns(postgres_dsn, "harness_executions")
        # RED today: 0009_fetched_urls does not exist yet, so "head" stops at 0008 and this key is
        # simply absent.
        assert "fetched_urls" in cols, "migration 0009 has not landed — head stops at 0008"
        col = cols["fetched_urls"]
        assert col["nullable"] is False
        assert col["type"] == "jsonb"
        assert col["default"] == "'[]'::jsonb"

        command.downgrade(cfg, "-1")
        cols_after_downgrade = _columns(postgres_dsn, "harness_executions")
        assert "fetched_urls" not in cols_after_downgrade
    finally:
        get_settings.cache_clear()
        _reset_schema(postgres_dsn)


# --- repository: create + the ordered/capped union on update_run -------------------------------


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


async def _create(repo: Any, *, fetched: list[str] | None) -> Any:
    return await repo.create(
        execution_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        harness_name="T",
        content_hash=None,
        status="SUCCEEDED",
        input_text="go",
        output="done",
        error_type=None,
        error_message=None,
        iterations=1,
        total_tokens=1,
        steps=[],
        fetched_urls=fetched,
    )


async def _update(repo: Any, row: Any, *, fetched: list[str] | None) -> Any:
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
        fetched_urls=fetched,
    )


async def test_a_run_that_fetched_nothing_records_an_empty_list_not_null(repo: Any) -> None:
    row = await _create(repo, fetched=None)
    assert row.fetched_urls == []
    fetched_row = await repo.get(row.id, _ORG)
    assert fetched_row.fetched_urls == []


async def test_the_created_set_round_trips_in_order(repo: Any) -> None:
    row = await _create(repo, fetched=[_A, _B])
    fetched_row = await repo.get(row.id, _ORG)
    assert list(fetched_row.fetched_urls) == [_A, _B]


async def test_a_resume_unions_the_new_segment_into_the_persisted_set(repo: Any) -> None:
    # Pre-pause fetched A; post-resume fetched B. Replacing would drop A, and a post-pause marker
    # numbered against A would resolve to nothing — a correct citation turned into a fabrication.
    row = await _create(repo, fetched=[_A])
    updated = await _update(repo, row, fetched=[_B])
    assert set(updated.fetched_urls) == {_A, _B}


async def test_the_union_preserves_first_seen_order(repo: Any) -> None:
    # Order is the numbering order — [S1]/[S2]/... — so it must never reshuffle across a resume.
    row = await _create(repo, fetched=[_A, _B])
    updated = await _update(repo, row, fetched=[_C])
    assert list(updated.fetched_urls) == [_A, _B, _C]


async def test_re_fetching_the_same_url_does_not_duplicate_it(repo: Any) -> None:
    row = await _create(repo, fetched=[_A, _B])
    updated = await _update(repo, row, fetched=[_B, _C])
    assert list(updated.fetched_urls) == [_A, _B, _C]


async def test_a_segment_that_fetched_nothing_leaves_the_set_intact(repo: Any) -> None:
    row = await _create(repo, fetched=[_A])
    updated = await _update(repo, row, fetched=[])
    assert list(updated.fetched_urls) == [_A]


async def test_omitting_the_set_on_an_update_leaves_it_intact(repo: Any) -> None:
    row = await _create(repo, fetched=[_A, _B])
    updated = await _update(repo, row, fetched=None)
    assert list(updated.fetched_urls) == [_A, _B]


async def test_the_union_survives_a_re_read(repo: Any) -> None:
    row = await _create(repo, fetched=[_A])
    await _update(repo, row, fetched=[_B])
    fetched_row = await repo.get(row.id, _ORG)
    assert set(fetched_row.fetched_urls) == {_A, _B}


async def test_at_the_cap_the_union_truncates_the_tail_keeping_the_head_stable(repo: Any) -> None:
    """T14: the cap is enforced at the REPOSITORY layer (the loop's own cap only bounds one
    segment's accumulator; a resume's union could otherwise grow past it forever). Truncating the
    TAIL — never the head — is what keeps every already-numbered ``[Sn]`` marker from a prior
    segment resolving to the same URL after the cap is reached.
    """
    from oraclous_harness_runtime_service.domain.loop.tool_use import _MAX_FETCHED_URLS

    head = [f"https://example.com/head-{i}" for i in range(_MAX_FETCHED_URLS - 1)]
    row = await _create(repo, fetched=head)
    assert len(row.fetched_urls) == _MAX_FETCHED_URLS - 1

    # One free slot left. Two new URLs arrive on resume — only the first fits under the cap.
    overflow_first = "https://example.com/overflow-0"
    overflow_second = "https://example.com/overflow-1"
    updated = await _update(repo, row, fetched=[overflow_first, overflow_second])

    assert len(updated.fetched_urls) == _MAX_FETCHED_URLS
    assert list(updated.fetched_urls)[: len(head)] == head  # head untouched, in order
    assert updated.fetched_urls[-1] == overflow_first  # the fitting one lands at the tail
    assert overflow_second not in updated.fetched_urls  # the one that didn't fit is dropped
