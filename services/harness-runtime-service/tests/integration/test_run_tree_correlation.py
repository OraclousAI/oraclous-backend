"""harness run-tree correlation vs real Postgres (ADR-037 Decision 3 / #471).

Proves the harness-side of the run-tree:
  * a root run (no trace_id passed) mints ``trace_id = execution_id``;
  * a child run threads the passed ``trace_id`` + ``parent_execution_id``;
  * H1/H4 — reads stay org-scoped even when two orgs' rows share a ``trace_id`` value, so a forged
    trace_id can never link or leak across a tenant boundary.

Key-free: a testcontainer Postgres; the repo self-binds the org (ADR-030 org_scope).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_DEV = "00000000-0000-0000-0000-00000000050a"
_OTHER = "00000000-0000-0000-0000-0000000006ff"


@pytest.fixture
async def repo(postgres_dsn: str) -> AsyncIterator[object]:
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


async def _create(
    repo: object,
    org: str,
    execution_id: uuid.UUID,
    *,
    trace_id: uuid.UUID | None = None,
    parent: uuid.UUID | None = None,
) -> object:
    return await repo.create(  # type: ignore[attr-defined]
        execution_id=execution_id,
        organisation_id=uuid.UUID(org),
        user_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        harness_name="T",
        content_hash=None,
        status="SUCCEEDED",
        input_text="go",
        output="ok",
        error_type=None,
        error_message=None,
        iterations=1,
        total_tokens=1,
        steps=[],
        trace_id=trace_id,
        parent_execution_id=parent,
    )


async def test_root_mints_trace_id_equal_to_execution_id(repo: object) -> None:
    rid = uuid.uuid4()
    root = await _create(repo, _DEV, rid)
    assert root.trace_id == rid  # type: ignore[attr-defined]  # a root is its own tree root
    assert root.parent_execution_id is None  # type: ignore[attr-defined]


async def test_child_threads_trace_and_parent(repo: object) -> None:
    rid, cid = uuid.uuid4(), uuid.uuid4()
    root = await _create(repo, _DEV, rid)
    child = await _create(repo, _DEV, cid, trace_id=root.trace_id, parent=rid)  # type: ignore[attr-defined]
    assert child.trace_id == rid  # type: ignore[attr-defined]  # shares the root's trace_id
    assert child.parent_execution_id == rid  # type: ignore[attr-defined]


@pytest.mark.security
async def test_get_is_org_scoped_despite_shared_trace_id(repo: object) -> None:
    """H1/H4: org-A reading by id cannot fetch org-B's row even when both carry the same trace_id
    value — get() filters organisation_id, so a forged trace_id never links across tenants."""
    shared, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _create(repo, _DEV, a, trace_id=shared)
    await _create(repo, _OTHER, b, trace_id=shared)
    assert (await repo.get(a, uuid.UUID(_DEV))) is not None  # type: ignore[attr-defined]
    assert (await repo.get(b, uuid.UUID(_DEV))) is None  # type: ignore[attr-defined]  # other org invisible
