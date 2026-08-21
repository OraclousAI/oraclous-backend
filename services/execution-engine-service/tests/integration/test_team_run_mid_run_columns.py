"""#828 + #832 — the new mid-run columns and the monotonicity guarantee, on a REAL Postgres.

The unit suite proves the drive emits the signals. What only the real substrate can prove is what
happens to the row:

* the three new columns round-trip through JSONB under the org-bound ``oraclous_app`` engine, so
  the RLS backstop admits the write (ADR-030);
* a pre-migration row reads ``{}`` rather than NULL, so a poll of an old run is not a 500. This is
  asserted rather than assumed because the server default is invisible at the call site: a
  migration that adds the column ``nullable=False`` with no ``server_default`` fails on existing
  rows, and one that adds it nullable leaves every historical row reading NULL;
* concurrent checkpoints leave ``results`` monotonic. #832 is a lost-update race, and a lost update
  is exactly the class of bug that passes in-memory and fails against a lock.

RED until migration ``0025`` and the ordering fix land.
"""

from __future__ import annotations

import asyncio
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
            {"role": "a", "kind": "agent", "manifest_ref": "org:x/a@1", "subgoal": "do a"},
            {"role": "b", "kind": "agent", "manifest_ref": "org:x/b@1", "subgoal": "do b"},
            {"role": "c", "kind": "agent", "manifest_ref": "org:x/c@1", "subgoal": "do c"},
        ],
        "runtime": {"entrypoint": "a"},
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


# ── the new columns round-trip ───────────────────────────────────────────────────────────────────


async def test_the_mid_run_columns_round_trip_through_jsonb(repo) -> None:  # noqa: ANN001
    row = await _running_row(repo, ORG_A)
    execution_id = str(uuid.uuid4())

    with org_scope(ORG_A):
        applied = await repo.checkpoint(
            row.id,
            ORG_A,
            member_status={"a": "succeeded", "b": "running"},
            member_timings={
                "a": {
                    "started_at": "2026-08-21T09:00:00+00:00",
                    "ended_at": "2026-08-21T09:04:00+00:00",
                },
                "b": {"started_at": "2026-08-21T09:04:00+00:00", "ended_at": None},
            },
            child_execution_roles={execution_id: "a"},
        )
        fetched = await repo.get(row.id, ORG_A)

    assert applied is True
    assert fetched is not None
    assert fetched.state == "RUNNING"  # a checkpoint is not a state transition
    assert fetched.member_status == {"a": "succeeded", "b": "running"}
    assert fetched.member_timings["b"]["ended_at"] is None
    assert fetched.child_execution_roles == {execution_id: "a"}


async def test_a_row_created_without_the_new_columns_reads_empty_not_null(repo) -> None:  # noqa: ANN001
    # The server default, which is what makes every run that predates the migration readable.
    row = await _running_row(repo, ORG_A)

    with org_scope(ORG_A):
        fetched = await repo.get(row.id, ORG_A)

    assert fetched is not None
    assert fetched.member_timings == {}
    assert fetched.child_execution_roles == {}


# ── #832: results never regresses ────────────────────────────────────────────────────────────────


async def test_concurrent_checkpoints_leave_results_monotonic(repo) -> None:  # noqa: ANN001
    # The lost-update race, forced against the real row lock. Three snapshots of increasing size
    # are submitted in reverse order of size; whichever commits last, the row must never end up
    # holding fewer members than a snapshot it already accepted.
    row = await _running_row(repo, ORG_A)
    snapshots = [
        {"a": {"output": "a-out"}},
        {"a": {"output": "a-out"}, "b": {"output": "b-out"}},
        {"a": {"output": "a-out"}, "b": {"output": "b-out"}, "c": {"output": "c-out"}},
    ]

    async def write(snapshot: dict[str, Any], delay: float) -> None:
        await asyncio.sleep(delay)
        with org_scope(ORG_A):
            await repo.checkpoint(
                row.id,
                ORG_A,
                results=snapshot,
                member_status={role: "succeeded" for role in snapshot},
            )

    # the biggest snapshot is submitted first, the smallest last
    await asyncio.gather(
        write(snapshots[2], 0.0),
        write(snapshots[1], 0.01),
        write(snapshots[0], 0.02),
    )

    with org_scope(ORG_A):
        fetched = await repo.get(row.id, ORG_A)

    assert fetched is not None
    assert set(fetched.results) == {"a", "b", "c"}, "the row lost a member it had already accepted"
    assert set(fetched.member_status) == {"a", "b", "c"}


async def test_a_killed_drive_leaves_results_and_member_status_in_agreement(repo) -> None:  # noqa: ANN001
    # #832's own acceptance clause. Every role the row calls succeeded must have an entry in
    # results, or /rerun re-dispatches an already-finished member: real spend, and that member's
    # side effects fire twice.
    row = await _running_row(repo, ORG_A)

    with org_scope(ORG_A):
        await repo.checkpoint(
            row.id,
            ORG_A,
            results={"a": {"output": "a-out"}, "b": {"output": "b-out"}},
            member_status={"a": "succeeded", "b": "succeeded"},
        )
        await repo.transition(
            row.id,
            ORG_A,
            new_state="FAILED",
            allowed_from=frozenset({"RUNNING"}),
            error_message="worker killed",
            member_status={"a": "succeeded", "b": "succeeded", "c": "failed"},
        )
        fetched = await repo.get(row.id, ORG_A)

    assert fetched is not None
    settled = {r for r, s in fetched.member_status.items() if s in ("succeeded", "partial")}
    assert settled <= set(fetched.results), "a member is marked succeeded with no output on the row"
