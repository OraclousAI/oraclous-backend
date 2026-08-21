"""#828 + #832 — the new mid-run columns and the monotonicity guarantee, on a REAL Postgres.

The unit suite proves the drive emits the signals. What only the real substrate can prove is what
happens to the row:

* the three new columns round-trip through JSONB under the org-bound ``oraclous_app`` engine, so
  the RLS backstop admits the write (ADR-030);
* a pre-migration row reads ``{}`` rather than NULL, so a poll of an old run is not a 500. This is
  asserted rather than assumed because the server default is invisible at the call site: a
  migration that adds the column ``nullable=False`` with no ``server_default`` fails on existing
  rows, and one that adds it nullable leaves every historical row reading NULL;
* two concurrent checkpoints never tear into a mixture of both snapshots, which is the row lock's
  own guarantee and the one the D3 in-process lock is layered on top of;
* a drive that dies leaves ``results`` and ``member_status`` agreeing, driven through the real
  service rather than asserted over data the test wrote itself.

**#832's ORDERING half is not tested here, on purpose.** The ruled fix (D3) is a per-run
``asyncio.Lock`` held across the snapshot and the emit, in process. A test that calls ``checkpoint``
directly skips that lock, so any ordering assertion at this layer could only pass if the repository
grew merge logic the fix does not add. Ordering is asserted where it is implemented:
``packages/ohm/tests/test_dispatch_signals.py``.

RED until migration ``0025`` lands.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.team_run_service import TeamRunService
from oraclous_governance import Principal, PrincipalType
from oraclous_substrate.access_async import org_scope

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _principal(org: uuid.UUID) -> Principal:
    return Principal(principal_id=USER_A, principal_type=PrincipalType.USER, organisation_id=org)


class _HarnessFailingB:
    """Every member succeeds except 'b'. The drive then takes its non-aborting failure path, which
    is the shape that leaves ``results`` and ``member_status`` free to disagree."""

    async def execute(self, **kw: Any) -> dict[str, Any]:
        ref = kw.get("manifest_ref") or ""
        role = ref.split("/")[-1].split("@")[0] if ref else ""
        out: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": f"{role}-out",
            "total_tokens": 100,
        }
        if role == "b":
            out["status"] = "FAILED"
            out["error_message"] = "b blew up"
        return out


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


@pytest.fixture
async def team_run_service(engine_dsns) -> AsyncIterator[TeamRunService]:  # noqa: ANN001
    """The REAL request-path service on the org-bound ``oraclous_app`` engine, wired as deployment
    wires it — so the failure path under test is the one that actually ships."""
    _owner_dsn, app_dsn = engine_dsns
    app_repo = TeamRunRepository(app_dsn)
    try:
        yield TeamRunService(team_runs=app_repo, harness=_HarnessFailingB())
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


# ── #832, at the layer that actually owns each half ──────────────────────────────────────────────
#
# Two properties, deliberately NOT tested here as one:
#
# ORDERING is the DRIVER's, and it is asserted in ``packages/ohm/tests/test_dispatch_signals.py``.
# The D3 fix is a per-run ``asyncio.Lock`` around the snapshot and the emit, in process. Calling
# ``checkpoint`` directly from a test skips that lock entirely, so a "snapshots commit in order"
# assertion at this layer could only pass if the repository grew merge logic — which the ruled fix
# does not add, and should not: ``checkpoint`` writing something other than what its caller handed
# it would make every other caller's write silently conditional.
#
# SERIALIZATION is the REPOSITORY's, and it is asserted below. The row lock guarantees two
# concurrent writes do not interleave into a mixture of both. That is real, it is this layer's job,
# and it is the property the in-process lock is layered on top of.


async def test_concurrent_checkpoints_never_interleave_into_a_mixed_row(repo) -> None:  # noqa: ANN001
    # GREEN TODAY, by design: this is a regression guard, not a reproducer. The row lock already
    # holds, and the D3 fix is layered on top of it rather than replacing it.
    #
    # The property, against a real Postgres. Two writers submit disjoint, internally-consistent
    # snapshots at once; whichever wins, the row must equal exactly ONE of them. A row holding
    # x's results beside y's member_status is a torn write, and no amount of in-process ordering
    # upstream can repair it. An implementation that drops the FOR UPDATE, or that widens
    # ``checkpoint`` into several statements outside one transaction, breaks this and nothing
    # else in the suite would notice.
    row = await _running_row(repo, ORG_A)
    x = {
        "results": {"a": {"output": "a-out"}},
        "member_status": {"a": "succeeded"},
        "cost_tokens": 100,
    }
    y = {
        "results": {"a": {"output": "a-out"}, "b": {"output": "b-out"}},
        "member_status": {"a": "succeeded", "b": "succeeded"},
        "cost_tokens": 200,
    }

    async def write(snapshot: dict[str, Any], delay: float) -> None:
        await asyncio.sleep(delay)
        with org_scope(ORG_A):
            await repo.checkpoint(row.id, ORG_A, **snapshot)

    await asyncio.gather(write(y, 0.0), write(x, 0.0))

    with org_scope(ORG_A):
        fetched = await repo.get(row.id, ORG_A)

    assert fetched is not None
    landed = {
        "results": fetched.results,
        "member_status": fetched.member_status,
        "cost_tokens": fetched.cost_tokens,
    }
    assert landed in (x, y), f"the row holds a mixture of two snapshots: {landed}"


async def test_a_drive_that_dies_leaves_results_and_member_status_in_agreement(
    team_run_service,  # noqa: ANN001
) -> None:
    # GREEN TODAY, by design: an invariant guard, not a reproducer. #832's stranding needs the
    # checkpoint race to land inside a kill window, which is not reliably forceable against a real
    # database. The reproducer is at the seam that owns the ordering,
    # ``test_checkpoint_snapshots_are_delivered_in_the_order_they_were_taken``.
    #
    # What this pins is #832's acceptance clause on the shipped failure path, over data the
    # PRODUCTION code wrote rather than data the test wrote itself. 'b' fails mid-stage, the drive
    # settles the row, and every role the row then calls succeeded must have an entry in results.
    # An implementation that repairs member_status on the failure path while leaving results stale
    # breaks this — which is exactly the asymmetry #832 describes.
    #
    # If it ever went red, /rerun would re-dispatch an already-finished member: real token spend,
    # and that member's side effects firing a second time.
    principal = _principal(ORG_A)
    created = await team_run_service.create(
        principal,
        manifest=_team(ORG_A),
        sub_harnesses={},
        gate_decisions={},
    )
    row = await team_run_service.drive(created.id, principal)

    # the guard against the test going vacuous: if 'b' ever stops failing, the failure path this
    # exists to cover is not being exercised at all and every assertion below passes for free.
    assert row.state == "FAILED", f"the drive did not take its failure path (state {row.state})"

    fetched = await team_run_service.get(row.id, principal)
    statuses = fetched.member_status or {}
    assert statuses.get("b") in ("failed", "blocked"), "the failing member was not recorded failed"
    settled = {r for r, s in statuses.items() if s in ("succeeded", "partial")}
    assert settled, "no member settled at all, so the assertion below proves nothing"
    assert settled <= set(fetched.results or {}), (
        "a member is marked succeeded with no output on the row"
    )
