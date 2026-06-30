"""Schedule orchestration (services layer).

The request path registers/lists/deletes schedules (org-scoped). The beat path calls ``fire_due``:
for each enabled cron schedule whose most-recent window hasn't been fired, it creates a QUEUED job
(idempotent on ``(org, idempotency_key=schedule:window)`` so a duplicate beat tick never re-fires)
+ enqueues it + advances ``last_fired_at``. A system (cross-org) sweep — each job is created under
its schedule's OWN org (ADR-006 carve-out, like the reaper).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from croniter import croniter
from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.adopted_tool_run import AdoptedToolRun
from oraclous_execution_engine_service.models.enums import BudgetPeriod, ScheduleType, TargetKind
from oraclous_execution_engine_service.models.schedule import EngineSchedule
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.repositories.maintenance_repository import (
    EngineMaintenanceRepository,
)
from oraclous_execution_engine_service.repositories.schedule_repository import ScheduleRepository
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.graph_client import GraphClient, GraphClientError
from oraclous_execution_engine_service.services.job_service import EnqueueFn

# An adopted-tool dispatch hand-off: (run_id, instance_id, input_data, organisation_id, user_id) →
# fire the registry-execute worker task. ``run_id`` is the engine_adopted_tool_runs row id, so the
# worker can stamp the registry execution_id back onto it. Injected (fire-and-forget, like the
# harness EnqueueFn) so a slow/down registry never blocks the cross-org Beat sweep. None on a path
# that should not dispatch (tests / mis-wire).
AdoptedToolEnqueueFn = Callable[[uuid.UUID, uuid.UUID, dict[str, Any], uuid.UUID, uuid.UUID], None]
# #601: a standing-team dispatch hand-off: (run_id, organisation_id, user_id) → drive the team-run
# worker. Same 3-UUID shape as the harness ``EnqueueFn`` (the team-run carries its own manifest +
# graph binding, so no extra payload). Injected like the others; None on a non-dispatch path.
TeamRunEnqueueFn = Callable[[uuid.UUID, uuid.UUID, uuid.UUID], None]


def _window_start(now: datetime, period: str) -> datetime:
    """#598: the UTC start of the budget window ``now`` falls in. NOT croniter — an independent
    CALENDAR boundary keyed off the fire wall-clock (croniter stays the fire-window cursor). A
    fire whose window-start exceeds the stored anchor has crossed the boundary → reset.
    daily = UTC midnight; weekly = ISO Monday 00:00 UTC; monthly = calendar-month start 00:00 UTC.
    Calendar-correct (variable month length, ISO week), never a naive 24h/7d/30d delta."""
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == BudgetPeriod.WEEKLY.value:
        return midnight - timedelta(days=now.weekday())  # weekday(): Monday=0
    if period == BudgetPeriod.MONTHLY.value:
        return midnight.replace(day=1)
    return midnight  # daily (the default + the safe fallback for an unknown period)


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
        enqueue_team_run: TeamRunEnqueueFn | None = None,
        team_runs: TeamRunRepository | None = None,
        graphs: GraphClient | None = None,
        maintenance: EngineMaintenanceRepository | None = None,
    ) -> None:
        self._schedules = schedules
        self._jobs = jobs
        self._provenance = provenance
        self._enqueue = enqueue
        # #601: the request-path KGS client — register fail-closes a team schedule bound to a
        # cross-org / non-existent graph_id (mirrors TeamRunService.create's check). None on the
        # Beat path (register is request-path only); then KGS RLS stays the authoritative scope.
        self._graphs = graphs
        # #601: the standing-team dispatch callback + the team-run repo (the team branch's
        # create-before-enqueue home). Like `_enqueue_adopted_tool`: both injected on the Beat
        # path AND the fire-now request path; None means the team branch creates the dedupe row +
        # advances the cursor but never dispatches — a mis-wire the DI must avoid (test-guarded).
        self._enqueue_team_run = enqueue_team_run
        self._team_runs = team_runs
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
        graph_id: str | None = None,
        budget_period: str | None = None,
        budget_allowance_tokens: int | None = None,
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
        elif target_kind == TargetKind.TEAM.value:
            # #601: a standing team carries an INLINE team manifest + a PERSISTENT graph workspace;
            # input_data carries the team-run spec ({sub_harnesses, gate_decisions}). instance_id is
            # adopted-tool-only. (manifest_ref resolution at fire time would need a registry client
            # here — out of scope; a standing team's manifest is stored inline.)
            if manifest_inline is None:
                raise ScheduleError("a team schedule requires an inline team manifest")
            if manifest_ref is not None:
                raise ScheduleError("a team schedule takes manifest_inline, not manifest_ref")
            if instance_id is not None:
                raise ScheduleError("instance_id is only for target_kind adopted_tool_run")
            if not graph_id:
                raise ScheduleError("a team schedule requires a graph_id (the graph workspace)")
            # fail-fast: the bound graph must exist in the caller's org (mirrors the request path),
            # so a cross-org / non-existent binding is rejected at register, not silently per fire.
            await self._validate_graph_id(org_id, graph_id)
        else:
            raise ScheduleError(f"unknown target_kind {target_kind!r}")
        # #598 (L3 per-period cap, team-only): period + allowance are all-or-nothing + fail-closed —
        # a period with no allowance (or vice versa) is a no-op cap (fail-OPEN), so it is rejected.
        budget_window_start = self._validate_budget(
            target_kind, type, budget_period, budget_allowance_tokens
        )
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
                graph_id=graph_id,
                budget_period=budget_period,
                budget_allowance_tokens=budget_allowance_tokens,
                budget_window_start=budget_window_start,
            )
            await self._emit(
                org_id, principal.principal_id, row.id, "engine.schedule.register", type
            )
        return row

    @staticmethod
    def _validate_budget(
        target_kind: str,
        type_: str,
        budget_period: str | None,
        budget_allowance_tokens: int | None,
    ) -> datetime | None:
        """#598: validate the L3 per-period cap (fail-closed) + return the window anchor to stamp.
        The cap is team-only + recurring (cron); period ∈ {daily,weekly,monthly}; period and
        allowance are paired (both or neither); allowance > 0. Returns the current window start when
        a cap is set (stamped at register so window 1 is anchored), else None (cap OFF)."""
        if budget_period is None and budget_allowance_tokens is None:
            return None  # default-OFF: the #585/#601 path is byte-identical
        if target_kind != TargetKind.TEAM.value:
            raise ScheduleError("a per-period budget cap is only for a team schedule")
        if type_ != ScheduleType.CRON.value:
            raise ScheduleError("a per-period budget cap requires a recurring (cron) schedule")
        if budget_period is None or budget_allowance_tokens is None:
            raise ScheduleError(
                "a per-period budget cap needs BOTH budget_period and budget_allowance_tokens"
            )
        if budget_period not in {p.value for p in BudgetPeriod}:
            raise ScheduleError("budget_period must be one of daily, weekly, monthly")
        if budget_allowance_tokens <= 0:
            raise ScheduleError("budget_allowance_tokens must be > 0")
        return _window_start(datetime.now(UTC), budget_period)

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
        elif sched.target_kind == TargetKind.TEAM.value:
            # #598 L3 pre-flight: roll the window if it crossed a boundary, then if the per-period
            # allowance is exhausted PAUSE the fleet + SKIP — returning BEFORE the cursor advance so
            # the un-fired window re-fires cleanly once the boundary sweep resumes the schedule.
            if not await self._budget_preflight(sched, now):
                return 0
            fired = await self._fire_team_run(sched, key)
        else:  # harness_job (the default; old rows read here)
            fired = await self._fire_harness_job(sched, key)
        # advance the cursor regardless (a duplicate-window create returned None but IS fired).
        await self._schedules.set_last_fired(sched.id, prev)
        return fired

    async def _budget_preflight(self, sched: EngineSchedule, now: datetime) -> bool:
        """#598 (ADR-044 L3 / ADR-048 dec 4b): the schedule-level per-period cap, checked at fire
        time. Returns True if the standing team may fire this window, False if its allowance is
        exhausted (and pauses the fleet). No-op (always True) when no cap is set — then the
        #585/#601 team fire path is byte-for-byte unchanged.

        Three steps, all keyed off the FIRE wall-clock ``now``: (0) IN-FLIGHT GUARD — if a prior
        fire's run for this schedule has not settled yet (its cost has not accrued), SKIP this fire
        without pausing or resetting, so the cap is always checked against CURRENT settled spend and
        can never be overrun by dispatched-but-unsettled runs (ADR-048 dec 4b: 'does not silently
        overrun'); this serialises a BUDGETED standing team's runs (also the right semantics — they
        share one graph workspace) and, by deferring the reset while a run straddles the boundary,
        keeps the straddling run's cost in the window it ran in. (1) if the window rolled since the
        stored anchor, zero the in-window accrual + advance the anchor (reset-at-boundary); (2) if
        the (post-reset) accrual >= allowance, disable+budget-pause the schedule and skip the fire
        (pause-the-fleet, surfaced via provenance — never a silent overrun)."""
        if sched.budget_period is None or sched.budget_allowance_tokens is None:
            return True
        if self._team_runs is not None and await self._team_runs.has_active_for_schedule(
            sched.id, sched.organisation_id
        ):
            return False  # a prior run is still in-flight — wait for it to settle + accrue
        accrued = sched.recurring_cost_tokens
        cur = _window_start(now, sched.budget_period)
        if sched.budget_window_start is None or cur > sched.budget_window_start:
            await self._schedules.reset_window(sched.id, sched.organisation_id, cur)
            accrued = 0  # the window just rolled — the in-window accrual is back to zero
        if accrued >= sched.budget_allowance_tokens:
            await self._schedules.pause_budget(sched.id, sched.organisation_id)
            await self._emit(
                sched.organisation_id,
                sched.user_id,
                sched.id,
                "engine.schedule.budget_pause",
                str(accrued),
            )
            return False
        return True

    async def resume_budget_paused(self, now: datetime) -> int:
        """#598 Beat sweep: re-enable standing teams L3 paused once their period window has rolled
        (the 'resumes on the next cadence window' of ADR-048 dec 4b). A budget-paused schedule is
        ``enabled=False`` so ``fire_due``/``list_enabled_cron`` can never see it — this cross-org
        sweep is the only path that resumes it. Reads across orgs on the OWNER engine, then resets
        each rolled schedule under its own ``org_scope`` (mirrors ``fire_due``)."""
        reader = self._maintenance
        if reader is None:  # the Beat path always injects it; fail loud if mis-wired
            raise ScheduleError("resume_budget_paused requires the maintenance (cross-org) reader")
        resumed = 0
        for sched in await reader.list_budget_paused():
            try:  # one bad row must not abort the cross-org sweep
                if sched.budget_period is None:  # defensive: a paused row always carries a period
                    continue
                cur = _window_start(now, sched.budget_period)
                if sched.budget_window_start is None or cur > sched.budget_window_start:
                    with org_scope(sched.organisation_id):
                        await self._schedules.reset_window(sched.id, sched.organisation_id, cur)
                        await self._emit(
                            sched.organisation_id,
                            sched.user_id,
                            sched.id,
                            "engine.schedule.budget_resume",
                            cur.isoformat(),
                        )
                    resumed += 1
            except Exception:  # noqa: BLE001, S112 — best-effort sweep; skip + continue
                continue
        return resumed

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

    async def _fire_team_run(self, sched: EngineSchedule, key: str) -> int:
        # #601 (mirrors _fire_adopted_tool): create the standing team's run
        # — the EngineTeamRun is written with (org, idempotency_key) BEFORE dispatch, so a duplicate
        # same-window tick / fire-now gets None and is skipped (NO second run). The run is bound to
        # the schedule's PERSISTENT graph_id, so run N+1 reads the state run N wrote (ADR-048
        # decision 2 / ADR-040), and carries schedule_id so its settled cost accrues back into the
        # schedule's per-cadence accumulator. The fire acts as the schedule OWNER (sched.user_id).
        if self._team_runs is None:  # defensive: the team branch needs the team-run repo
            return 0
        spec = sched.input_data or {}
        run = await self._team_runs.create_scheduled(
            organisation_id=sched.organisation_id,
            user_id=sched.user_id,
            manifest=sched.manifest_inline or {},
            sub_harnesses=spec.get("sub_harnesses") or {},
            gate_decisions=spec.get("gate_decisions") or {},
            graph_id=sched.graph_id,
            schedule_id=sched.id,
            idempotency_key=key,
        )
        if run is not None and self._enqueue_team_run is not None:
            self._enqueue_team_run(run.id, sched.organisation_id, sched.user_id)
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

    async def list_team_runs(
        self, schedule_id: uuid.UUID, principal: Principal
    ) -> list[EngineTeamRun]:
        """#601: the team-runs a standing-team schedule produced (org-scoped, newest-first) — the
        readable proof it fired + the persistent graph each run is bound to (the keystone)."""
        org_id = self._require_org(principal)
        if self._team_runs is None:
            return []
        with org_scope(org_id):
            return await self._team_runs.list_for_schedule(schedule_id, org_id)

    async def _validate_graph_id(self, organisation_id: uuid.UUID, graph_id: str) -> None:
        """#601 (mirrors TeamRunService): the bound graph_id MUST exist in the caller's org. The KGS
        GET is org-scoped by the engine's downstream headers, so a graph the org does not own → a
        clear 4xx at register, not a confusing mid-fire member failure. With no graphs client wired
        (the Beat path) the check is skipped (KGS RLS stays the authoritative scope)."""
        if self._graphs is None:
            return
        try:
            exists = await self._graphs.graph_exists(graph_id)
        except GraphClientError as exc:  # KGS unreachable / inconclusive — fail closed (not admit)
            raise ScheduleError(
                "could not validate graph_id (knowledge-graph unreachable)"
            ) from exc
        if not exists:
            raise ScheduleError("graph_id does not exist in your organisation")

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
