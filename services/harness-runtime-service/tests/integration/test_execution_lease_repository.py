"""#1072 — the cross-replica cancel lease, vs real Postgres.

``ExecutionLeaseRepository`` (``repositories/execution_lease_repository.py``, not yet built) backs
the cancel signal that has to cross Helm's ``replicas: 2`` (design: no Redis in harness-runtime, so
an in-process registry is wrong). One row per in-flight execution, keyed by the GLOBALLY unique
``execution_id`` (the engine mints it once per dispatch and a duplicate is a 409 — see #1072 design
ruling, "Identifier"): ``harness_execution_leases(execution_id PK, organisation_id, created_at,
cancel_requested_at NULL)``, RLS-scoped like migration 0006.

Proves, against the repository directly (org-scoping at the app layer — the RLS backstop itself is
proven separately in ``tests/organization_isolation/test_harness_rls_backstop_isolation.py``):

* ``create`` + ``request_cancel`` under the owning org flips the flag and ``is_cancel_requested``
  sees it;
* ``request_cancel`` under a DIFFERENT org for the SAME execution_id returns False and never sets
  the flag (H1/H4 — a forged/guessed id from another org can't request a cancel);
* a second ``create`` for an execution_id already claimed raises ``DuplicateExecutionId`` — the
  global-PK conflict the service maps to 409 on ``execute()``;
* ``release`` deletes the row outright (the watcher's cleanup after the terminal row lands).

Key-free: a testcontainer Postgres; the repo self-binds the org (ADR-030 ``org_scope``), same
pattern as ``test_run_tree_correlation.py``. Every name new to this slice (the module, both classes,
all four repository methods, the table itself) is imported/referenced function-locally per
``.claude/rules/tests-seam-imports.md`` — this hard-fails RED (``ModuleNotFoundError``) until the
``[impl]`` lands, never a skip.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_ORG_A = uuid.UUID("00000000-0000-0000-0000-000000001072")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-000000002072")


def _row_count(dsn: str, execution_id: uuid.UUID) -> int:
    """Direct-SQL proof that ``release`` actually deleted the row (not just that the repository API
    stopped reporting it) — the superuser owner DSN, used only to observe, never to bypass the
    repository under test."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM harness_execution_leases WHERE execution_id = %s",
            (execution_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


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

    from oraclous_harness_runtime_service.repositories.execution_lease_repository import (
        ExecutionLeaseRepository,
    )

    r = ExecutionLeaseRepository(async_dsn)
    yield r
    await r.close()  # type: ignore[attr-defined]


async def test_create_then_request_cancel_under_owning_org_sets_the_flag(repo: object) -> None:
    execution_id = uuid.uuid4()
    await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]

    assert await repo.is_cancel_requested(execution_id) is False  # type: ignore[attr-defined]  # not requested yet

    flipped = await repo.request_cancel(  # type: ignore[attr-defined]
        execution_id=execution_id, organisation_id=_ORG_A
    )
    assert flipped is True
    assert await repo.is_cancel_requested(execution_id) is True  # type: ignore[attr-defined]


@pytest.mark.security
async def test_request_cancel_under_a_different_org_is_refused_and_flag_stays_unset(
    repo: object,
) -> None:
    """H1/H4: org B guessing/forging org A's execution_id cannot request a cancel on it."""
    execution_id = uuid.uuid4()
    await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]

    flipped = await repo.request_cancel(  # type: ignore[attr-defined]
        execution_id=execution_id, organisation_id=_ORG_B
    )
    assert flipped is False
    assert await repo.is_cancel_requested(execution_id) is False  # type: ignore[attr-defined]  # still unset


async def test_duplicate_execution_id_raises(repo: object) -> None:
    from oraclous_harness_runtime_service.repositories.execution_lease_repository import (
        DuplicateExecutionId,
    )

    execution_id = uuid.uuid4()
    await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]

    with pytest.raises(DuplicateExecutionId):
        await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]


async def test_release_deletes_the_row(repo: object, postgres_dsn: str) -> None:
    execution_id = uuid.uuid4()
    await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]
    assert _row_count(postgres_dsn, execution_id) == 1

    await repo.release(execution_id)  # type: ignore[attr-defined]

    assert _row_count(postgres_dsn, execution_id) == 0
    # a released id no longer claims anything — request_cancel finds no row for any org.
    assert (
        await repo.request_cancel(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]
    ) is False
