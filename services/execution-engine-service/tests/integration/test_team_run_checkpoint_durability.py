"""#819 — the checkpoint write against a REAL Postgres: durable, org-scoped, and lease-refreshing.

The unit tests prove the drive CALLS ``checkpoint``. What can only be proven on the real substrate
is what the write does to the row:

* it lands under the org-bound ``oraclous_app`` engine, so the RLS backstop admits it (ADR-030);
* it bumps ``updated_at``, so a run that is actively completing members stops looking stale to the
  3900-second lease sweep. That is a deliberate consequence of decision 1 on the issue: the lease
  exists to catch a DEAD driver, and a run emitting checkpoints is demonstrably alive. A run wedged
  inside one long member emits nothing and is still reaped, which is the case the lease was written
  for. The property is asserted rather than assumed because it is invisible at the call site: an
  implementation that passes ``updated_at`` through explicitly to "preserve" it, or that writes
  through textual SQL, silently drops it while every unit test stays green. (A SQLAlchemy Core
  ``update()`` against the mapped table is fine — the column's ``onupdate`` default still applies.
  Neither the ORM nor Core is being ruled out here; only the outcome is pinned);
* it refuses a row that is not RUNNING, so a settled run can never be rewritten by a late write
  from a driver that has already lost the race.

RED until ``TeamRunRepository.checkpoint`` lands.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_substrate.access_async import org_scope

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
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
            {
                "role": "b",
                "kind": "agent",
                "manifest_ref": "org:x/b@1",
                "subgoal": "do b",
                "depends_on": ["a"],
            },
        ],
        "runtime": {"entrypoint": "a"},
    }


@pytest.fixture
async def repos(engine_dsns) -> AsyncIterator[tuple[TeamRunRepository, TeamRunRepository]]:  # noqa: ANN001
    """(app, owner) — the org-bound request-path repo and the cross-org maintenance/reaper repo."""
    owner_dsn, app_dsn = engine_dsns
    app_repo, owner_repo = TeamRunRepository(app_dsn), TeamRunRepository(owner_dsn)
    try:
        yield app_repo, owner_repo
    finally:
        await app_repo.close()
        await owner_repo.close()


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


async def test_a_checkpoint_persists_the_partial_state_on_a_running_row(repos) -> None:  # noqa: ANN001
    app_repo, _ = repos
    row = await _running_row(app_repo, ORG_A)

    with org_scope(ORG_A):
        applied = await app_repo.checkpoint(
            row.id,
            ORG_A,
            results={"a": {"output": "a-out"}},
            member_status={"a": "succeeded"},
            cost_tokens=100,
        )
        fetched = await app_repo.get(row.id, ORG_A)

    assert applied is True
    assert fetched is not None
    assert fetched.state == "RUNNING"  # a checkpoint is not a state transition
    assert fetched.results == {"a": {"output": "a-out"}}
    assert fetched.member_status == {"a": "succeeded"}
    assert fetched.cost_tokens == 100


async def test_a_checkpoint_refreshes_the_reaper_lease(repos) -> None:  # noqa: ANN001
    # A run that is still completing members must stop looking stale. Take a cutoff AFTER the row
    # was claimed RUNNING (so it is stale by that cutoff), checkpoint, and it must drop out of the
    # sweep — the model's `onupdate=func.now()` applying to the checkpoint write like any other.
    app_repo, owner_repo = repos
    row = await _running_row(app_repo, ORG_A)

    await asyncio.sleep(0.05)
    cutoff = _dt.datetime.now(_dt.UTC)
    stale_before = await owner_repo.list_stale_running(cutoff)
    assert row.id in {r.id for r in stale_before}, "the claimed row was not stale to begin with"

    await asyncio.sleep(0.05)
    with org_scope(ORG_A):
        await app_repo.checkpoint(row.id, ORG_A, member_status={"a": "succeeded"})

    stale_after = await owner_repo.list_stale_running(cutoff)
    assert row.id not in {r.id for r in stale_after}  # a live run is no longer reap-eligible


async def test_a_wedged_run_that_never_checkpoints_is_still_reaped(repos) -> None:  # noqa: ANN001
    # The other half of the property: the lease still catches a driver stuck inside one long
    # member. Refreshing on checkpoint must not turn the sweep off.
    app_repo, owner_repo = repos
    row = await _running_row(app_repo, ORG_A)

    await asyncio.sleep(0.05)
    stale = await owner_repo.list_stale_running(_dt.datetime.now(_dt.UTC))
    assert row.id in {r.id for r in stale}


async def test_a_checkpoint_refuses_a_settled_row(repos) -> None:  # noqa: ANN001
    # A late write from a driver that already lost the race must not rewrite a terminal run.
    app_repo, _ = repos
    row = await _running_row(app_repo, ORG_A)
    with org_scope(ORG_A):
        await app_repo.transition(
            row.id,
            ORG_A,
            new_state="SUCCEEDED",
            allowed_from=frozenset({"RUNNING"}),
            results={"a": {"output": "final"}},
        )
        applied = await app_repo.checkpoint(row.id, ORG_A, results={"a": {"output": "stale"}})
        fetched = await app_repo.get(row.id, ORG_A)

    assert applied is False
    assert fetched is not None and fetched.results == {"a": {"output": "final"}}


async def test_a_checkpoint_cannot_cross_organisations(repos) -> None:  # noqa: ANN001
    # ADR-006/ADR-030: the new write is org-scoped like every other. Another tenant's id is a
    # no-op, never a cross-org overwrite.
    app_repo, _ = repos
    row = await _running_row(app_repo, ORG_A)

    with org_scope(ORG_B):
        applied = await app_repo.checkpoint(row.id, ORG_B, member_status={"a": "succeeded"})
    with org_scope(ORG_A):
        fetched = await app_repo.get(row.id, ORG_A)

    assert applied is False
    assert fetched is not None and fetched.member_status == {}
