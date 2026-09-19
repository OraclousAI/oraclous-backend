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
from oraclous_execution_engine_service.services.team_draft_service import TeamDraftService
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

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


# ── #1169: the settle-time save races the console's from-run — ONE draft ─────
#
# A build run that goes SUCCEEDED is now saved by the worker (``TeamDraftService.save_from_run``)
# while the console may fire ``create_from_run`` for the same run at the same moment (or a Celery
# redelivery may save twice). Everything below goes through the NEW ``save_from_run`` seam on a
# real ``TeamDraftService`` over the real Postgres-backed repository, so it is RED until it exists
# and then proves the partial unique serialises the callers into one row.

ORG_B = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

_ONE_MEMBER = (
    '{"members": [{"role": "researcher", "kind": "agent", "subgoal": "research",'
    ' "outputs_schema": {"required": ["summary"]}}]}'
)


class _RunRow:
    """A SUCCEEDED compile-run row: only the reviewer's compiled team matters to the saver."""

    def __init__(self, organisation_id: uuid.UUID) -> None:
        self.id = uuid.uuid4()
        self.organisation_id = organisation_id
        self.state = "SUCCEEDED"
        self.results = {"reviewer": {"output": _ONE_MEMBER, "status": "SUCCEEDED"}}
        self.manifest = {"metadata": {"name": "harness-compiler"}}
        self.member_error_codes: dict[str, str] | None = None


class _FakeTeamRuns:
    """The org-scoped run lookup the console's ``create_from_run`` performs (404 cross-org)."""

    def __init__(self, run: _RunRow) -> None:
        self._run = run

    async def get(self, run_id: uuid.UUID, principal: Principal) -> _RunRow:
        if run_id != self._run.id or principal.organisation_id != self._run.organisation_id:
            raise TeamRunError("team run not found", 404)
        return self._run


def _service(repo: TeamDraftRepository, run: _RunRow) -> TeamDraftService:
    return TeamDraftService(
        drafts=repo,
        team_runs=_FakeTeamRuns(run),  # type: ignore[arg-type]  # duck-typed run lookup
        refine_nl_poll_seconds=0.2,
        refine_nl_poll_interval_seconds=0.01,
    )


def _principal(org: uuid.UUID) -> Principal:
    return Principal(principal_id=USER, principal_type=PrincipalType.USER, organisation_id=org)


@pytest.fixture
async def count_for_run(engine_dsns) -> AsyncIterator[Any]:  # noqa: ANN001
    """Rows in ``engine_team_drafts`` for a run, counted as the table OWNER (RLS does not apply),
    so "exactly one row exists" is the whole table's truth, not one org's view of it."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    owner_async_dsn, _app = engine_dsns
    owner = create_async_engine(owner_async_dsn)

    async def _count(run_id: uuid.UUID) -> int:
        async with owner.connect() as conn:
            res = await conn.execute(
                text("SELECT count(*) FROM engine_team_drafts WHERE team_run_id = :r"),
                {"r": run_id},
            )
            return int(res.scalar_one())

    try:
        yield _count
    finally:
        await owner.dispose()


async def test_settle_save_and_console_from_run_racing_yield_one_draft(
    repo: TeamDraftRepository, count_for_run: Any
) -> None:
    run = _RunRow(ORG)
    svc = _service(repo, run)
    (row_s, _v_s, created_s), (row_c, _v_c, created_c) = await asyncio.gather(
        svc.save_from_run(run_row=run, org=ORG, user_id=USER),
        svc.create_from_run(_principal(ORG), team_run_id=run.id),
    )
    assert row_s.id == row_c.id  # ONE draft, whichever caller won
    assert created_s != created_c  # exactly one of them minted it
    with org_scope(ORG):
        found = await repo.get_by_team_run(ORG, run.id)
    assert found is not None and found.id == row_s.id
    assert await count_for_run(run.id) == 1


async def test_console_from_run_after_the_settle_save_returns_the_same_draft_not_created(
    repo: TeamDraftRepository, count_for_run: Any
) -> None:
    run = _RunRow(ORG)
    svc = _service(repo, run)
    saved, _v1, created_settle = await svc.save_from_run(run_row=run, org=ORG, user_id=USER)
    again, _v2, created_console = await svc.create_from_run(_principal(ORG), team_run_id=run.id)
    assert created_settle is True and created_console is False  # the console's 200, not a 201
    assert again.id == saved.id
    assert await count_for_run(run.id) == 1


async def test_two_concurrent_settle_saves_for_the_same_run_yield_one_draft(
    repo: TeamDraftRepository, count_for_run: Any
) -> None:
    # a Celery redelivery re-runs the settle tail: both saves must converge on one row
    run = _RunRow(ORG)
    svc = _service(repo, run)
    (row_a, _va, created_a), (row_b, _vb, created_b) = await asyncio.gather(
        svc.save_from_run(run_row=run, org=ORG, user_id=USER),
        svc.save_from_run(run_row=run, org=ORG, user_id=USER),
    )
    assert row_a.id == row_b.id
    assert created_a != created_b
    assert await count_for_run(run.id) == 1


async def test_a_settle_save_under_one_org_is_invisible_to_another_org(
    repo: TeamDraftRepository, count_for_run: Any
) -> None:
    run = _RunRow(ORG)
    svc = _service(repo, run)
    saved, _verdict, created = await svc.save_from_run(run_row=run, org=ORG, user_id=USER)
    assert created is True
    assert saved.organisation_id == ORG  # the row carries the run's org, never another
    # read through the org-bound app engine as org B: RLS + the org filter hide it
    with org_scope(ORG_B):
        assert await repo.get_by_team_run(ORG_B, run.id) is None
        assert await repo.get(saved.id, ORG_B) is None
    assert await count_for_run(run.id) == 1  # one row in the whole table
