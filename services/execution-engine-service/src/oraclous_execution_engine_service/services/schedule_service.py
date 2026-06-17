"""Schedule orchestration (ORAA-4 §21 services layer).

The request path registers/lists/deletes schedules (org-scoped). The beat path calls ``fire_due``:
for each enabled cron schedule whose most-recent window hasn't been fired, it creates a QUEUED job
(idempotent on ``(org, idempotency_key=schedule:window)`` so a duplicate beat tick never re-fires)
+ enqueues it + advances ``last_fired_at``. A system (cross-org) sweep — each job is created under
its schedule's OWN org (ADR-006 carve-out, like the reaper).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from croniter import croniter
from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.enums import ScheduleType
from oraclous_execution_engine_service.models.schedule import EngineSchedule
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.repositories.maintenance_repository import (
    EngineMaintenanceRepository,
)
from oraclous_execution_engine_service.repositories.schedule_repository import ScheduleRepository
from oraclous_execution_engine_service.services.job_service import EnqueueFn


class ScheduleError(Exception):
    """A schedule could not be registered/deleted (bad cron, no org, not found). HTTP 4xx."""


class ScheduleService:
    def __init__(
        self,
        *,
        schedules: ScheduleRepository,
        jobs: JobRepository,
        provenance: ProvenanceCollector,
        enqueue: EnqueueFn | None = None,
        maintenance: EngineMaintenanceRepository | None = None,
    ) -> None:
        self._schedules = schedules
        self._jobs = jobs
        self._provenance = provenance
        self._enqueue = enqueue
        # ADR-030 §3: the Beat path injects the OWNER-engine cross-org reader. list_enabled_cron
        # reads ACROSS orgs on it (FORCE'd RLS on the org-bound engine would fail it closed); each
        # due schedule then fires its job + advances its cursor on the ORG-BOUND repos under
        # org_scope(sched.org). None on the request path (register/list/delete are org-bound).
        self._maintenance = maintenance

    async def register(
        self,
        principal: Principal,
        *,
        type: str,
        input_text: str,
        manifest_inline: dict | None = None,
        manifest_ref: str | None = None,
        cron: str | None = None,
    ) -> EngineSchedule:
        org_id = self._require_org(principal)
        if (manifest_inline is None) == (manifest_ref is None):
            raise ScheduleError("supply exactly one of manifest (inline) or manifest_ref")
        if type == ScheduleType.CRON.value and (not cron or not croniter.is_valid(cron)):
            raise ScheduleError("a cron schedule requires a valid cron expression")
        # ADR-030 §3: bind the org for the whole request-path op so the org-bound engine's
        # begin-guard sets the GUC — else FORCE'd RLS rejects the INSERT (42501). Org from the
        # principal only (T1-M1), the same chokepoint the Beat per-schedule fire uses.
        with org_scope(org_id):
            row = await self._schedules.create(
                organisation_id=org_id,
                user_id=principal.principal_id,
                type=type,
                manifest_inline=manifest_inline,
                manifest_ref=manifest_ref,
                input_text=input_text,
                cron=cron,
            )
            await self._emit(
                org_id, principal.principal_id, row.id, "engine.schedule.register", type
            )
        return row

    async def list_schedules(self, principal: Principal) -> list[EngineSchedule]:
        org_id = self._require_org(principal)
        # ADR-030 §3: bind the org so the read runs with the GUC set (else FORCE'd RLS → zero rows).
        with org_scope(org_id):
            return await self._schedules.list_for_org(org_id)

    async def delete(self, schedule_id: uuid.UUID, principal: Principal) -> None:
        org_id = self._require_org(principal)
        # ADR-030 §3: bind the org so the DELETE (USING) + provenance write run with the GUC set.
        with org_scope(org_id):
            if not await self._schedules.delete(schedule_id, org_id):
                raise ScheduleError("schedule not found")
            await self._emit(
                org_id, principal.principal_id, schedule_id, "engine.schedule.delete", "ok"
            )

    async def fire_due(self, now: datetime) -> int:
        """Beat sweep: fire every enabled cron schedule whose latest window hasn't fired yet.

        ADR-030 §3 two-engine carve: the enabled-cron ENUMERATION reads across orgs on the OWNER
        engine (``self._maintenance``) — FORCE'd RLS on the org-bound engine would fail it closed to
        zero rows, so no org's cron would ever fire. Each due schedule is then fired on the
        ORG-BOUND ``self._jobs`` / ``self._schedules`` repos INSIDE ``org_scope(sched.org)`` (the
        job create + the set_last_fired cursor + the provenance write bind that org's GUC; a
        cross-org write is denied 42501). The schedule's org comes from the trusted maintenance
        read, never request input (T1-M1)."""
        reader = self._maintenance
        if reader is None:  # the Beat path always injects it; fail loud if mis-wired
            raise ScheduleError("fire_due requires the maintenance (cross-org) reader")
        fired = 0
        for sched in await reader.list_enabled_cron():
            try:  # one bad schedule must NOT abort the whole cross-org tick (cf. the reaper)
                with org_scope(sched.organisation_id):
                    fired += await self._fire_one(sched, now)
            except Exception:  # noqa: BLE001, S112 — best-effort sweep; skip this schedule, continue
                continue
        return fired

    async def _fire_one(self, sched: EngineSchedule, now: datetime) -> int:
        # is_valid can pass for an impossible date (e.g. Feb 30) on which get_prev raises — the
        # surrounding try in fire_due isolates that so it never stalls the other orgs' schedules.
        if not sched.cron or not croniter.is_valid(sched.cron):
            return 0
        prev = croniter(sched.cron, now).get_prev(datetime)  # most recent window strictly < now
        # defensive: keep the comparison aware-vs-aware across croniter versions.
        prev = prev if prev.tzinfo else prev.replace(tzinfo=UTC)
        if sched.last_fired_at is not None and sched.last_fired_at >= prev:
            return 0  # already fired this window
        key = f"{sched.id}:{prev.isoformat()}"
        job = await self._jobs.create_scheduled(
            organisation_id=sched.organisation_id,
            user_id=sched.user_id,
            manifest_inline=sched.manifest_inline,
            manifest_ref=sched.manifest_ref,
            input_text=sched.input_text,
            schedule_id=sched.id,
            idempotency_key=key,
        )
        fired = 0
        if job is not None and self._enqueue is not None:
            self._enqueue(job.id, sched.organisation_id, sched.user_id)
            await self._emit(
                sched.organisation_id, sched.user_id, sched.id, "engine.schedule.fire", key
            )
            fired = 1
        # advance the cursor regardless (a duplicate-window create returned None but is fired).
        await self._schedules.set_last_fired(sched.id, prev)
        return fired

    @staticmethod
    def _require_org(principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:
            raise ScheduleError("authenticated principal has no organisation scope")
        return principal.organisation_id

    async def _emit(
        self,
        org_id: uuid.UUID,
        principal_id: uuid.UUID,
        schedule_id: uuid.UUID,
        action: str,
        outcome: str,
    ) -> None:
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=str(principal_id),
                action=action,
                resource=f"engine_schedule:{schedule_id}",
                outcome=outcome,
            )
        )
