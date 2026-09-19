"""#1163 — the ``team_draft_id``/``team_draft_version`` columns, on a REAL Postgres.

A team run records which team draft (and which version) it was started from (R1/R2). Two things
only a real substrate can prove, mirroring ``test_team_run_skip_reasons_column.py`` one generation
back:

* the pair round-trips under the org-bound ``oraclous_app`` engine (ADR-030), and a run created
  without them reads ``None``/``None``;
* the pair CHECK (``ck_engine_team_runs_team_draft_pair``) is enforced by Postgres itself, not just
  application code — one column set without the other raises ``IntegrityError``;
* a rerun's CAS (QUEUED -> FAILED -> QUEUED, the same pattern ``rerun`` uses) never disturbs the
  columns, because ``transition`` only ever sets the fields it is explicitly given.

RED until migration ``0033`` and the mapped columns/constraint land (I1). Until then,
``repo.create(..., team_draft_id=..., team_draft_version=...)`` raises ``TypeError`` — the
constructor keyword arguments do not exist yet.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_substrate.access_async import org_scope
from sqlalchemy.exc import IntegrityError

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _team(org: uuid.UUID) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "team",
            "owner_organization_id": str(org),
            "kind": "team",
        },
        "members": [
            {
                "role": "scout",
                "kind": "agent",
                "manifest_ref": "org:x/scout@1",
                "subgoal": "scout",
            },
            {
                "role": "drafter",
                "kind": "agent",
                "manifest_ref": "org:x/drafter@1",
                "subgoal": "draft",
            },
        ],
        "runtime": {"entrypoint": "scout"},
    }


@pytest.fixture
async def repo(engine_dsns) -> AsyncIterator[TeamRunRepository]:  # noqa: ANN001
    _owner_dsn, app_dsn = engine_dsns
    app_repo = TeamRunRepository(app_dsn)
    try:
        yield app_repo
    finally:
        await app_repo.close()


async def test_team_draft_id_and_version_round_trip(repo) -> None:  # noqa: ANN001
    draft_id = uuid.uuid4()

    with org_scope(ORG_A):
        row = await repo.create(
            organisation_id=ORG_A,
            user_id=USER_A,
            manifest=_team(ORG_A),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=draft_id,
            team_draft_version=3,
        )
        fetched = await repo.get(row.id, ORG_A)

    assert fetched is not None
    assert fetched.team_draft_id == draft_id
    assert fetched.team_draft_version == 3


async def test_a_run_created_without_a_team_draft_reads_none_none(repo) -> None:  # noqa: ANN001
    with org_scope(ORG_A):
        row = await repo.create(
            organisation_id=ORG_A,
            user_id=USER_A,
            manifest=_team(ORG_A),
            sub_harnesses={},
            gate_decisions={},
        )
        fetched = await repo.get(row.id, ORG_A)

    assert fetched is not None
    assert fetched.team_draft_id is None
    assert fetched.team_draft_version is None


async def test_id_without_version_is_rejected_by_the_pair_check(repo) -> None:  # noqa: ANN001
    with pytest.raises(IntegrityError):
        with org_scope(ORG_A):
            await repo.create(
                organisation_id=ORG_A,
                user_id=USER_A,
                manifest=_team(ORG_A),
                sub_harnesses={},
                gate_decisions={},
                team_draft_id=uuid.uuid4(),
            )


async def test_a_rerun_cas_keeps_the_team_draft_pair(repo) -> None:  # noqa: ANN001
    draft_id = uuid.uuid4()

    with org_scope(ORG_A):
        row = await repo.create(
            organisation_id=ORG_A,
            user_id=USER_A,
            manifest=_team(ORG_A),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=draft_id,
            team_draft_version=3,
        )
        failed, applied_fail = await repo.transition(
            row.id, ORG_A, new_state="FAILED", allowed_from=frozenset({"QUEUED"})
        )
        assert applied_fail and failed is not None
        requeued, applied_requeue = await repo.transition(
            row.id, ORG_A, new_state="QUEUED", allowed_from=frozenset({"FAILED"})
        )
        assert applied_requeue and requeued is not None
        fetched = await repo.get(row.id, ORG_A)

    assert fetched is not None
    assert fetched.team_draft_id == draft_id
    assert fetched.team_draft_version == 3
