"""from-run idempotency vs REAL Postgres + the RLS backstop (#638 concern 3) — docker-required.

The partial-unique ``(organisation_id, team_run_id) WHERE team_run_id IS NOT NULL`` + the
``TeamDraftRepository.create_from_run`` idempotent insert, on the org-bound ``oraclous_app``
engine. Proven HERE against real concurrency: two concurrent from-runs for the SAME run yield ONE
row (the loser returns the winner's committed draft); a repeat is idempotent; and a DIRECT draft
(``team_run_id`` NULL) is NOT constrained by the partial unique.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.repositories.team_draft_repository import (
    TeamDraftRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation, pytest.mark.isolation]

ORG = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
USER = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _manifest(name: str = "compiled") -> dict[str, Any]:
    return {"ohm_version": "1.1", "metadata": {"name": name, "kind": "team"}, "members": []}


@pytest.fixture
async def repo(engine_dsns) -> AsyncIterator[TeamDraftRepository]:  # noqa: ANN001
    _owner, app_async_dsn = engine_dsns
    r = TeamDraftRepository(app_async_dsn)
    try:
        yield r
    finally:
        await r.close()


async def _from_run(repo: TeamDraftRepository, run_id: uuid.UUID, name: str) -> tuple[Any, bool]:
    with org_scope(ORG):  # each concurrent call binds its own org (contextvar per task)
        return await repo.create_from_run(
            organisation_id=ORG,
            user_id=USER,
            name=name,
            manifest=_manifest(name),
            sub_harnesses={},
            team_run_id=run_id,
        )


async def test_two_concurrent_from_runs_for_the_same_run_yield_one_row(
    repo: TeamDraftRepository,
) -> None:
    run_id = uuid.uuid4()
    (row_a, created_a), (row_b, created_b) = await asyncio.gather(
        _from_run(repo, run_id, "a"), _from_run(repo, run_id, "b")
    )
    # exactly one insert won; the other returned the winner's committed row (same id)
    assert created_a != created_b  # one True, one False — the partial unique serialised the race
    assert row_a.id == row_b.id  # ONE draft
    with org_scope(ORG):
        found = await repo.get_by_team_run(ORG, run_id)
    assert found is not None and found.id == row_a.id


async def test_a_repeat_from_run_is_idempotent(repo: TeamDraftRepository) -> None:
    run_id = uuid.uuid4()
    first, c1 = await _from_run(repo, run_id, "first")
    second, c2 = await _from_run(repo, run_id, "second")
    assert c1 is True and c2 is False  # first mints, repeat returns the existing
    assert second.id == first.id and second.name == "first"  # the existing draft, unchanged


async def test_direct_drafts_are_not_constrained_by_the_partial_unique(
    repo: TeamDraftRepository,
) -> None:
    # two directly-created drafts (team_run_id NULL) coexist — the partial unique fires only on a
    # NOT-NULL team_run_id, so the from-run dedupe never constrains the normal create path.
    with org_scope(ORG):
        d1 = await repo.create(
            organisation_id=ORG, user_id=USER, name="d1", manifest=_manifest(), sub_harnesses={}
        )
        d2 = await repo.create(
            organisation_id=ORG, user_id=USER, name="d2", manifest=_manifest(), sub_harnesses={}
        )
    assert d1.id != d2.id and d1.team_run_id is None and d2.team_run_id is None
