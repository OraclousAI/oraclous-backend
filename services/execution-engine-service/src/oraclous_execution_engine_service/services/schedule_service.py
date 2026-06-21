"""Schedule orchestration (ORAA-4 §21 services layer).

The request path registers/lists/deletes schedules (org-scoped). The beat path calls ``fire_due``:
for each enabled cron schedule whose most-recent window hasn't been fired, it creates a QUEUED job
(idempotent on ``(org, idempotency_key=schedule:window)`` so a duplicate beat tick never re-fires)
+ enqueues it + advances ``last_fired_at``. A system (cross-org) sweep — each job is created under
its schedule's OWN org (ADR-006 carve-out, like the reaper).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from croniter import croniter
from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.adopted_tool_run import AdoptedToolRun
from oraclous_execution_engine_service.models.enums import ScheduleType, TargetKind
from oraclous_execution_engine_service.models.schedule import EngineSchedule
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.repositories.maintenance_repository import (
    EngineMaintenanceRepository,
)
from oraclous_execution_engine_service.repositories.schedule_repository import ScheduleRepository
from oraclous_execution_engine_service.services.job_service import EnqueueFn

# An adopted-tool dispatch hand-off: (run_id, instance_id, input_data, organisation_id, user_id) →
# fire the registry-execute worker task. ``run_id`` is the engine_adopted_tool_runs row id, so the
# worker can stamp the registry execution_id back onto it. Injected (fire-and-forget, like the
# harness EnqueueFn) so a slow/down registry never blocks the cross-org Beat sweep. None on a path
# that should not dispatch (tests / mis-wire).
AdoptedToolEnqueueFn = Callable[[uuid.UUID, uuid.UUID, dict[str, Any], uuid.UUID, uuid.UUID], None]


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
        enqueue_adopted_tool: AdoptedToolEnqueueFn | None = None,
        maintenance: EngineMaintenanceRepository | None = None,
    ) -> None:
        self._schedules = schedules
        self._jobs = jobs
        self._provenance = provenance
        self._enqueue = enqueue
        # The adopted-tool dispatch callback (#489). Parallel to `_enqueue`: injected on the Beat
        # path AND the fire-now request path; None means the adopted_tool_run branch cannot dispatch
        # (so it would create the dedupe row + advance the cursor but never fire — a mis-wire the
        # request-path DI must avoid, guarded by a test).
        self._enqueue_adopted_tool = enqueue_adopted_tool
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
        target_kind: str = TargetKind.HARNESS_JOB.value,
        manifest_inline: dict | None = None,
        manifest_ref: str | None = None,
        cron: str | None = None,
        instance_id: uuid.UUID | None = None,
        input_data: dict | None = None,
    ) -> EngineSchedule:
        org_id = self._require_org(principal)
        # The manifest-exclusivity rule is CONDITIONAL on target_kind (#489): harness_job keeps the
        # exactly-one-manifest rule; adopted_tool_run forbids both manifests + requires instance_id.
        if target_kind == TargetKind.HARNESS_JOB.value:
            if (manifest_inline is None) == (manifest_ref is None):
                raise ScheduleError("supply exactly one of manifest (inline) or manifest_ref")
            if instance_id is not None:
                raise ScheduleError("instance_id is only for target_kind adopted_tool_run")
        elif target_kind == TargetKind.ADOPTED_TOOL_RUN.value:
            if manifest_inline is not None or manifest_ref is not None:
                raise ScheduleError("an adopted_tool_run schedule takes no manifest/manifest_ref")
            if instance_id is None:
                raise ScheduleError("an adopted_tool_run schedule requires instance_id")
        else:
            raise ScheduleError(f"unknown target_kind {target_kind!r}")
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
                target_kind=target_kind,
                instance_id=instance_id,
                input_data=input_data,
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
        if sched.target_kind == TargetKind.ADOPTED_TOOL_RUN.value:
            fired = await self._fire_adopted_tool(sched, key)
        else:  # harness_job (the default; old rows read here)
            fired = await self._fire_harness_job(sched, key)
        # advance the cursor regardless (a duplicate-window create returned None but IS fired).
        await self._schedules.set_last_fired(sched.id, prev)
        return fired

    async def _fire_harness_job(self, sched: EngineSchedule, key: str) -> int:
        job = await self._jobs.create_scheduled(
            organisation_id=sched.organisation_id,
            user_id=sched.user_id,
            manifest_inline=sched.manifest_inline,
            manifest_ref=sched.manifest_ref,
            input_text=sched.input_text,
            schedule_id=sched.id,
            idempotency_key=key,
        )
        if job is not None and self._enqueue is not None:
            self._enqueue(job.id, sched.organisation_id, sched.user_id)
            await self._emit(
                sched.organisation_id, sched.user_id, sched.id, "engine.schedule.fire", key
            )
            return 1
        return 0

    async def _fire_adopted_tool(self, sched: EngineSchedule, key: str) -> int:
        # Create-idempotent-BEFORE-dispatch (#489): the unique (org, idempotency_key) row is written
        # TRANSACTIONALLY before any registry dispatch is enqueued, so a duplicate same-window
        # tick / fire-now gets None here and is skipped — NO second execution. Only when fresh
        # (run is not None) AND a dispatch callback is wired do we enqueue the registry execute. The
        # dispatch carries the schedule OWNER (sched.user_id) + sched.organisation_id (no SYSTEM
        # principal exists; the auto-fire acts as the owner), so registry org-scoping + credential
        # resolution run under the right tenant.
        if sched.instance_id is None:  # defensive: an adopted_tool_run schedule must carry one
            return 0
        run = await self._jobs.create_adopted_tool_run(
            organisation_id=sched.organisation_id,
            schedule_id=sched.id,
            idempotency_key=key,
        )
        if run is not None and self._enqueue_adopted_tool is not None:
            self._enqueue_adopted_tool(
                run.id,
                sched.instance_id,
                sched.input_data or {},
                sched.organisation_id,
                sched.user_id,
            )
            await self._emit(
                sched.organisation_id, sched.user_id, sched.id, "engine.schedule.fire", key
            )
            return 1
        return 0

    async def fire_now(self, schedule_id: uuid.UUID, principal: Principal) -> EngineSchedule:
        """Manual fire of a schedule's CURRENT window without waiting for a Beat tick (#489). Reuses
        the SAME branch + idempotency + cursor logic as the Beat path (``_fire_one``), so a second
        same-window fire-now is a no-op (the dedupe row blocks the second dispatch). Allowed on
        ``cron`` and ``manual`` schedules (the window is computed from now either way). Returns the
        refreshed schedule row (cursor advanced)."""
        org_id = self._require_org(principal)
        with org_scope(org_id):
            sched = await self._schedules.get(schedule_id, org_id)
            if sched is None:
                raise ScheduleError("schedule not found")
            await self._fire_one(sched, datetime.now(UTC))
            refreshed = await self._schedules.get(schedule_id, org_id)
            if refreshed is None:  # pragma: no cover — deleted mid-fire; treat as not-found
                raise ScheduleError("schedule not found")
            return refreshed

    async def list_adopted_runs(
        self, schedule_id: uuid.UUID, principal: Principal
    ) -> list[AdoptedToolRun]:
        """The adopted-tool-run rows a schedule produced (org-scoped) — the readable proof a
        schedule fired + the registry execution_id(s). Asserts the no-double-fire guarantee."""
        org_id = self._require_org(principal)
        with org_scope(org_id):
            return await self._jobs.list_adopted_runs_for_schedule(schedule_id, org_id)

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
