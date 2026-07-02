"""#501-#1: the adopted-tool dispatch CLAIM is atomic under CONCURRENT Postgres transactions.

The worker's exactly-once gate is ``JobRepository.claim_adopted_dispatch`` — a single conditional
``UPDATE … WHERE execution_id IS NULL AND (dispatched_at IS NULL OR dispatched_at < now-lease)
RETURNING id``. Its correctness rests on Postgres serialising two concurrent UPDATEs on one row (the
second blocks on the first's row lock, then re-evaluates its WHERE against the committed version via
EvalPlanQual and matches zero rows). The unit test can only assert the worker CALLS the claim — its
in-memory fake is atomic by construction. This proves the REAL DB-level atomicity: two genuinely
concurrent claims (each on its OWN engine/connection) on one row → EXACTLY ONE wins.

Real ``oraclous_app`` org-bound engine (FORCE'd RLS), org bound via ``org_scope`` — so it also
exercises the claim under the deployed tenancy backstop, not a superuser. Threats: T1-M1 (the double
dispatch is a within-org exactly-once property, proven on the real substrate).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.repositories.job_repository import JobRepository

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_LEASE = 300


@pytest.fixture
async def app_repo(engine_dsns) -> AsyncIterator[str]:  # noqa: ANN001
    """Yield the app (org-bound, FORCE'd-RLS) async DSN — each test builds its OWN repos on it, so
    two claims run on genuinely separate connections/transactions."""
    _owner_async_dsn, app_async_dsn = engine_dsns
    yield app_async_dsn


async def _seed_run(dsn: str) -> uuid.UUID:
    """Create one unclaimed adopted-tool-run row (org-scoped) and return its id."""
    jobs = JobRepository(dsn)
    try:
        with org_scope(_ORG):
            run = await jobs.create_adopted_tool_run(
                organisation_id=_ORG, schedule_id=uuid.uuid4(), idempotency_key=str(uuid.uuid4())
            )
        assert run is not None
        return run.id
    finally:
        await jobs.close()


async def test_two_concurrent_claims_on_one_row_exactly_one_wins(app_repo: str) -> None:
    # THE atomicity proof: two claims on SEPARATE engines (→ separate connections → genuinely
    # concurrent transactions) race for the same row. Postgres row-locks the first UPDATE; the
    # second blocks, then re-checks its WHERE against the committed row (dispatched_at fresh) → 0.
    run_id = await _seed_run(app_repo)
    jobs_a, jobs_b = JobRepository(app_repo), JobRepository(app_repo)
    now = datetime.now(UTC)
    try:
        with org_scope(_ORG):  # gathered children inherit the bound org (contextvar copy)
            results = await asyncio.gather(
                jobs_a.claim_adopted_dispatch(run_id, _ORG, now=now, lease_seconds=_LEASE),
                jobs_b.claim_adopted_dispatch(run_id, _ORG, now=now, lease_seconds=_LEASE),
            )
    finally:
        await jobs_a.close()
        await jobs_b.close()

    assert results.count(True) == 1, f"exactly one concurrent claim must win — got {results}"
    assert results.count(False) == 1

    # the row is now claimed (dispatched_at stamped) — a fresh claim within the lease then loses.
    jobs_c = JobRepository(app_repo)
    try:
        with org_scope(_ORG):
            again = await jobs_c.claim_adopted_dispatch(run_id, _ORG, now=now, lease_seconds=_LEASE)
    finally:
        await jobs_c.close()
    assert again is False, "a second claim within the lease must lose (dispatch already claimed)"


async def test_a_stale_claim_is_reclaimable_after_the_lease(app_repo: str) -> None:
    # crash-mid-execute recovery: a claim that never stamped execution_id ages past the lease and
    # becomes re-claimable, so the reaper's re-fire can proceed. Simulate by claiming at T0, then
    # claiming again with a 'now' well past T0 + lease.
    run_id = await _seed_run(app_repo)
    jobs = JobRepository(app_repo)
    t0 = datetime.now(UTC)
    try:
        with org_scope(_ORG):
            first = await jobs.claim_adopted_dispatch(run_id, _ORG, now=t0, lease_seconds=_LEASE)
            # a claim whose 'now' is far past t0 sees dispatched_at (t0) < now-lease → re-claimable.
            future = t0.replace(year=t0.year + 1)  # unambiguously past any lease
            reclaimed = await jobs.claim_adopted_dispatch(
                run_id, _ORG, now=future, lease_seconds=_LEASE
            )
    finally:
        await jobs.close()
    assert first is True and reclaimed is True, "a stale claim must be re-claimable past the lease"


async def test_claim_loses_once_execution_id_is_stamped(app_repo: str) -> None:
    # once a dispatch stamped its execution_id (a completed run), no claim can win — the run is
    # done, so a redelivery/re-fire never re-executes.
    run_id = await _seed_run(app_repo)
    jobs = JobRepository(app_repo)
    now = datetime.now(UTC)
    try:
        with org_scope(_ORG):
            await jobs.set_adopted_execution_id(run_id, _ORG, uuid.uuid4())  # a completed dispatch
            claimed = await jobs.claim_adopted_dispatch(run_id, _ORG, now=now, lease_seconds=_LEASE)
    finally:
        await jobs.close()
    assert claimed is False, "a stamped (completed) run is never re-claimable"
