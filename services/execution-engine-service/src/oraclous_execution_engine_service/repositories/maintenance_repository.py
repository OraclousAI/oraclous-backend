"""Cross-org MAINTENANCE reader (ORAA-4 §21 repositories layer; ADR-030 §3 carve-out).

The three cross-org sweeps run OUT-OF-REQUEST with NO bound org — the reaper
(``JobRepository.list_stale_running`` + ``RoundtableRepository.list_stale_running``) and Celery Beat
(``ScheduleRepository.list_enabled_cron``). Under FORCE'd RLS on the org-bound ``oraclous_app``
engine those reads would fail closed to zero rows (T1-M1), so a dead worker's RUNNING job / a
stranded round-table / a due cron schedule would never be found.

So those reads are carved onto a SEPARATE engine here — the MAINTENANCE engine, the OWNER (or a
BYPASSRLS) role, which reads ACROSS orgs. This mirrors the auth-service split (ADR-012 §1a): the
cross-org producer (auth's credential store) connects on the owner DSN and is NOT RLS-scoped, while
the org-bound identity engine carries the guard. This repo is that owner-engine reader for the
engine service: it builds its engine with the org-GUC guard DELIBERATELY NOT installed
(``install_guard``
is left off on the underlying repos) and on the owner DSN, so the cross-org read is admitted.

It holds ONLY the read half of the maintenance sweeps. The per-row settle/transition AFTER a sweep
(time-out, re-queue, fire-a-job, advance-the-cursor, the provenance event) does NOT happen here — it
goes back through the ORG-BOUND repositories wrapped in ``org_scope(row.organisation_id)`` (the
service layer does this), so every WRITE is RLS-scoped to the row's own org and a cross-org write is
denied (SQLSTATE 42501). No row crosses a tenant boundary: the owner engine is read-only here, used
only to enumerate the work, with each unit then settled under its own org.

It composes the existing repositories (each constructed with ``install_guard=False`` on the owner
DSN) rather than re-implementing the queries, so the cross-org SELECTs live in exactly one place
(the org-bound and maintenance paths run the identical SQL, only the engine/role differs).
"""

from __future__ import annotations

from datetime import datetime

from oraclous_execution_engine_service.models.job import EngineJob
from oraclous_execution_engine_service.models.roundtable import EngineRoundtable
from oraclous_execution_engine_service.models.schedule import EngineSchedule
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.repositories.roundtable_repository import (
    RoundtableRepository,
)
from oraclous_execution_engine_service.repositories.schedule_repository import ScheduleRepository


class EngineMaintenanceRepository:
    """Read-only cross-org enumerator for the reaper + Beat sweeps, on the OWNER engine.

    ``maintenance_url`` is the owner (BYPASSRLS) async DSN (Settings.maintenance_url). Built with
    NullPool — the reaper/beat tasks own + dispose it per tick, like the org-bound worker repos.
    """

    def __init__(self, maintenance_url: str) -> None:
        # install_guard=False: the owner role bypasses RLS, so the org-GUC guard would be inert;
        # skipping it keeps this engine plainly the cross-org reader (mirrors auth's owner-engine
        # credential store, which has no guard). worker_pool=True → NullPool (per-tick lifecycle).
        self._jobs = JobRepository(maintenance_url, worker_pool=True, install_guard=False)
        self._roundtables = RoundtableRepository(
            maintenance_url, worker_pool=True, install_guard=False
        )
        self._schedules = ScheduleRepository(maintenance_url, worker_pool=True, install_guard=False)

    async def close(self) -> None:
        await self._jobs.close()
        await self._roundtables.close()
        await self._schedules.close()

    async def list_stale_jobs(self, older_than: datetime, *, limit: int = 100) -> list[EngineJob]:
        """RUNNING jobs whose lease has expired — across ALL orgs (the reaper's read)."""
        return await self._jobs.list_stale_running(older_than, limit=limit)

    async def list_stale_roundtables(
        self, older_than: datetime, *, limit: int = 100
    ) -> list[EngineRoundtable]:
        """RUNNING round-tables whose driver died mid-turn — across ALL orgs (the reaper's read)."""
        return await self._roundtables.list_stale_running(older_than, limit=limit)

    async def list_enabled_cron(self, *, limit: int = 500) -> list[EngineSchedule]:
        """All enabled cron schedules — across ALL orgs (Beat's read)."""
        return await self._schedules.list_enabled_cron(limit=limit)
