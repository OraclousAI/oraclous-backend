"""#1119 (#1154) — the ``member_skip_reasons`` column, on a REAL Postgres.

The team-run graph read (#1154) reports a closed set of skip codes per member —
``condition_false`` | ``condition_source_missing`` | ``condition_error`` recorded by the runtime
when the run happens, plus ``unrecorded`` for a run made before this column existed. That last
value is the one this file exists to guarantee: whether a pre-migration row reads ``{}`` (never
``NULL``, never a raise) is exactly what decides whether the graph read can fall back to
``unrecorded`` for it, or whether it 500s on an old run instead. Two things only a real substrate
can prove, mirroring ``test_team_run_mid_run_columns.py`` one generation back:

* the column round-trips through JSONB under the org-bound ``oraclous_app`` engine, so the RLS
  backstop admits the write (ADR-030);
* a row with nothing written to the column reads ``{}`` rather than NULL or raising — the server
  default that makes a run predating the migration safe to read. This is asserted rather than
  assumed because the server default is invisible at the call site: a migration that adds the
  column ``nullable=False`` with no ``server_default`` fails on existing rows, and one that adds it
  nullable leaves every historical row reading NULL.

RED until migration ``0032`` and the mapped ``EngineTeamRun.member_skip_reasons`` column land.
Until then, ``checkpoint``'s generic ``**fields`` sets an unpersisted plain attribute on the ORM
instance it was called on (no declared column, so no SQL error at write time); a FRESH fetch
(``repo.get``, a new query) returns a new instance that never had it set, so reading
``fetched.member_skip_reasons`` raises ``AttributeError``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_substrate.access_async import org_scope

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


async def _running_row(repo: TeamRunRepository, org: uuid.UUID) -> Any:
    with org_scope(org):
        row = await repo.create(
            organisation_id=org,
            user_id=USER_A,
            manifest=_team(org),
            sub_harnesses={},
            gate_decisions={},
        )
        claimed, applied = await repo.transition(
            row.id, org, new_state="RUNNING", allowed_from=frozenset({"QUEUED"})
        )
    assert applied and claimed is not None
    return claimed


async def test_member_skip_reasons_round_trips_through_jsonb(repo) -> None:  # noqa: ANN001
    row = await _running_row(repo, ORG_A)
    written = {"drafter": {"code": "condition_false", "role": "scout"}}

    with org_scope(ORG_A):
        applied = await repo.checkpoint(row.id, ORG_A, member_skip_reasons=written)
        fetched = await repo.get(row.id, ORG_A)

    assert applied is True
    assert fetched is not None
    assert fetched.state == "RUNNING"  # a checkpoint is not a state transition
    assert fetched.member_skip_reasons == written


async def test_row_without_column_reads_empty_not_null(repo) -> None:  # noqa: ANN001
    # The server default, which is what makes every run that predates the migration readable — and
    # what lets the graph read (#1154) report ``unrecorded`` instead of 500ing on an old run.
    row = await _running_row(repo, ORG_A)

    with org_scope(ORG_A):
        fetched = await repo.get(row.id, ORG_A)

    assert fetched is not None
    assert fetched.member_skip_reasons == {}
