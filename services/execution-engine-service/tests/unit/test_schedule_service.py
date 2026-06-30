"""ScheduleService — register/list/delete + the idempotent beat fire_due (fakes, real croniter)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from oraclous_execution_engine_service.models.schedule import EngineSchedule
from oraclous_execution_engine_service.services.schedule_service import (
    ScheduleError,
    ScheduleService,
)
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_NOW = datetime(2026, 6, 7, 12, 0, 30, tzinfo=UTC)
_PREV = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)  # the minute boundary <= _NOW


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


def _schedule(*, cron: str | None, last_fired: datetime | None = None) -> EngineSchedule:
    return EngineSchedule(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        type="cron",
        cron=cron,
        manifest_ref="harness-123",
        input_text="go",
        enabled=True,
        last_fired_at=last_fired,
        target_kind="harness_job",
        instance_id=None,
        input_data=None,
    )


_INSTANCE = uuid.uuid4()


def _adopted_schedule(*, cron: str | None, last_fired: datetime | None = None) -> EngineSchedule:
    return EngineSchedule(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        type="cron",
        cron=cron,
        manifest_ref=None,
        input_text="scheduled",
        enabled=True,
        last_fired_at=last_fired,
        target_kind="adopted_tool_run",
        instance_id=_INSTANCE,
        input_data={"channel": "email", "content": "weekly digest"},
    )


def _team_schedule(
    *, cron: str | None, last_fired: datetime | None = None, graph_id: str | None = "graph-1"
) -> EngineSchedule:
    return EngineSchedule(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        type="cron",
        cron=cron,
        manifest_inline={"members": []},  # an inline team manifest (validated downstream)
        manifest_ref=None,
        input_text="standing team",
        enabled=True,
        last_fired_at=last_fired,
        target_kind="team",
        instance_id=None,
        input_data={"sub_harnesses": {}, "gate_decisions": {}},
        graph_id=graph_id,
    )


class _FakeSchedRepo:
    def __init__(self, rows: list[EngineSchedule] | None = None) -> None:
        self.rows = rows or []

    async def create(self, **kw: object) -> EngineSchedule:
        row = EngineSchedule(id=uuid.uuid4(), enabled=True, last_fired_at=None, **kw)
        self.rows.append(row)
        return row

    async def list_for_org(self, org: uuid.UUID, *, limit: int = 100) -> list[EngineSchedule]:
        return [r for r in self.rows if r.organisation_id == org]

    async def get(self, schedule_id: uuid.UUID, org: uuid.UUID) -> EngineSchedule | None:
        return next(
            (r for r in self.rows if r.id == schedule_id and r.organisation_id == org), None
        )

    async def delete(self, schedule_id: uuid.UUID, org: uuid.UUID) -> bool:
        before = len(self.rows)
        self.rows = [r for r in self.rows if not (r.id == schedule_id and r.organisation_id == org)]
        return len(self.rows) < before

    async def list_enabled_cron(self, *, limit: int = 500) -> list[EngineSchedule]:
        return [r for r in self.rows if r.type == "cron" and r.enabled]

    async def set_last_fired(self, schedule_id: uuid.UUID, fired_at: datetime) -> None:
        for r in self.rows:
            if r.id == schedule_id:
                r.last_fired_at = fired_at


class _FakeJobRepo:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.seen: set[str] = set()
        # adopted-tool-run idempotency ledger (#489): the (org, key) unique row
        self.adopted_created: list[str] = []
        self.adopted_seen: set[str] = set()
        self.adopted_rows: list[SimpleNamespace] = []

    async def create_scheduled(self, *, idempotency_key: str, **_kw: object):  # noqa: ANN202
        if idempotency_key in self.seen:  # the (org, key) unique constraint
            return None
        self.seen.add(idempotency_key)
        self.created.append(idempotency_key)
        return SimpleNamespace(id=uuid.uuid4())

    async def create_adopted_tool_run(  # noqa: ANN202
        self, *, organisation_id: uuid.UUID, schedule_id: uuid.UUID, idempotency_key: str
    ):
        # mirror the real (org, idempotency_key) unique constraint: a duplicate window → None
        if idempotency_key in self.adopted_seen:
            return None
        self.adopted_seen.add(idempotency_key)
        self.adopted_created.append(idempotency_key)
        row = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            schedule_id=schedule_id,
            idempotency_key=idempotency_key,
            execution_id=None,
        )
        self.adopted_rows.append(row)
        return row

    async def set_adopted_execution_id(
        self, run_id: uuid.UUID, organisation_id: uuid.UUID, execution_id: uuid.UUID
    ) -> None:
        for r in self.adopted_rows:
            if r.id == run_id and r.organisation_id == organisation_id:
                r.execution_id = execution_id

    async def list_adopted_runs_for_schedule(  # noqa: ANN202
        self, schedule_id: uuid.UUID, organisation_id: uuid.UUID, *, limit: int = 100
    ):
        return [
            r
            for r in self.adopted_rows
            if r.schedule_id == schedule_id and r.organisation_id == organisation_id
        ]


class _FakeTeamRunRepo:
    """#601: the create-before-enqueue dedupe ledger for scheduled team fires — mirrors the real
    partial unique (org, idempotency_key): a duplicate same-window create returns None."""

    def __init__(self) -> None:
        self.team_created: list[str] = []
        self.team_seen: set[str] = set()
        self.team_rows: list[SimpleNamespace] = []

    async def create_scheduled(  # noqa: ANN202
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        manifest: dict,
        sub_harnesses: dict,
        gate_decisions: dict,
        graph_id: str | None,
        schedule_id: uuid.UUID,
        idempotency_key: str,
    ):
        if idempotency_key in self.team_seen:  # the partial (org, idempotency_key) unique
            return None
        self.team_seen.add(idempotency_key)
        self.team_created.append(idempotency_key)
        row = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            schedule_id=schedule_id,
            graph_id=graph_id,
            idempotency_key=idempotency_key,
        )
        self.team_rows.append(row)
        return row

    async def list_for_schedule(  # noqa: ANN202
        self, schedule_id: uuid.UUID, organisation_id: uuid.UUID, *, limit: int = 100
    ):
        return [
            r
            for r in self.team_rows
            if r.schedule_id == schedule_id and r.organisation_id == organisation_id
        ]


class _FakeProv:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def emit(self, record) -> None:  # noqa: ANN001
        self.events.append(record.action)


class _FakeMaintenance:
    """ADR-030 §3 cross-org reader fake: fire_due enumerates enabled cron schedules from the owner
    engine here, then fires each on the org-bound repos under org_scope. Forwards to the same
    schedule repo so the test's single store is the source of truth."""

    def __init__(self, srepo: _FakeSchedRepo) -> None:
        self._srepo = srepo

    async def list_enabled_cron(self, *, limit: int = 500) -> list[EngineSchedule]:
        return await self._srepo.list_enabled_cron(limit=limit)


def _svc(
    srepo: _FakeSchedRepo,
    jrepo: _FakeJobRepo,
    enqueue=None,  # noqa: ANN001
    enqueue_adopted_tool=None,  # noqa: ANN001
    enqueue_team_run=None,  # noqa: ANN001
    team_runs: _FakeTeamRunRepo | None = None,
) -> tuple[ScheduleService, _FakeProv]:
    prov = _FakeProv()
    svc = ScheduleService(
        schedules=srepo,  # type: ignore[arg-type]
        jobs=jrepo,  # type: ignore[arg-type]
        provenance=prov,  # type: ignore[arg-type]
        enqueue=enqueue,
        enqueue_adopted_tool=enqueue_adopted_tool,
        enqueue_team_run=enqueue_team_run,
        team_runs=team_runs,  # type: ignore[arg-type]
        maintenance=_FakeMaintenance(srepo),  # type: ignore[arg-type]
    )
    return svc, prov


# ── register / list / delete ──────────────────────────────────────────────────────────────────────
async def test_register_cron_valid() -> None:
    svc, prov = _svc(_FakeSchedRepo(), _FakeJobRepo())
    row = await svc.register(
        _principal(), type="cron", manifest_ref="h", input_text="go", cron="*/5 * * * *"
    )
    assert row.cron == "*/5 * * * *"
    assert "engine.schedule.register" in prov.events


async def test_register_cron_without_expression_raises() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError):
        await svc.register(_principal(), type="cron", manifest_ref="h", input_text="go", cron=None)


async def test_register_invalid_cron_raises() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError):
        await svc.register(
            _principal(), type="cron", manifest_ref="h", input_text="go", cron="not a cron"
        )


async def test_register_requires_exactly_one_manifest() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError):  # neither inline nor ref
        await svc.register(_principal(), type="cron", input_text="go", cron="* * * * *")
    with pytest.raises(ScheduleError):  # both
        await svc.register(
            _principal(),
            type="cron",
            manifest_inline={"x": 1},
            manifest_ref="h",
            input_text="go",
            cron="* * * * *",
        )


async def test_register_inline_manifest() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    row = await svc.register(
        _principal(),
        type="cron",
        manifest_inline={"ohm_version": "1.0"},
        input_text="go",
        cron="*/2 * * * *",
    )
    assert row.manifest_inline == {"ohm_version": "1.0"} and row.manifest_ref is None


async def test_delete_missing_raises() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError):
        await svc.delete(uuid.uuid4(), _principal())


async def test_no_org_scope_raises() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError):
        await svc.list_schedules(_principal(org=None))


# ── fire_due (beat) ─────────────────────────────────────────────────────────────────────────────
async def test_fire_due_fires_a_due_schedule() -> None:
    srepo = _FakeSchedRepo([_schedule(cron="* * * * *", last_fired=None)])
    jrepo = _FakeJobRepo()
    calls: list = []
    svc, prov = _svc(srepo, jrepo, enqueue=lambda j, o, u: calls.append(j))
    fired = await svc.fire_due(_NOW)
    assert fired == 1 and len(jrepo.created) == 1 and len(calls) == 1
    assert srepo.rows[0].last_fired_at == _PREV  # cursor advanced
    assert "engine.schedule.fire" in prov.events


async def test_fire_due_skips_already_fired_window() -> None:
    srepo = _FakeSchedRepo([_schedule(cron="* * * * *", last_fired=_PREV)])
    jrepo = _FakeJobRepo()
    calls: list = []
    svc, _ = _svc(srepo, jrepo, enqueue=lambda j, o, u: calls.append(j))
    fired = await svc.fire_due(_NOW)
    assert fired == 0 and jrepo.created == [] and calls == []


async def test_fire_due_isolates_a_bad_cron_and_still_fires_the_rest() -> None:
    # `0 0 30 2 *` (Feb 30) passes croniter.is_valid but raises on get_prev — it must NOT abort the
    # sweep and stall every other org's schedule.
    bad = _schedule(cron="0 0 30 2 *", last_fired=None)
    good = _schedule(cron="* * * * *", last_fired=None)
    srepo = _FakeSchedRepo([bad, good])
    jrepo = _FakeJobRepo()
    calls: list = []
    svc, _ = _svc(srepo, jrepo, enqueue=lambda j, o, u: calls.append(j))
    fired = await svc.fire_due(_NOW)
    assert fired == 1 and len(calls) == 1  # the good schedule fired despite the bad one


async def test_fire_due_idempotent_create_advances_without_double_enqueue() -> None:
    sched = _schedule(cron="* * * * *", last_fired=None)
    srepo = _FakeSchedRepo([sched])
    jrepo = _FakeJobRepo()
    jrepo.seen.add(f"{sched.id}:{_PREV.isoformat()}")  # another beat already fired this window
    calls: list = []
    svc, _ = _svc(srepo, jrepo, enqueue=lambda j, o, u: calls.append(j))
    fired = await svc.fire_due(_NOW)
    assert fired == 0 and calls == []  # no double fire
    assert srepo.rows[0].last_fired_at == _PREV  # but the cursor still advances


# ── register validation: target_kind × manifest combinations (#489) ──────────────────────────────
async def test_register_adopted_tool_run_valid() -> None:
    svc, prov = _svc(_FakeSchedRepo(), _FakeJobRepo())
    row = await svc.register(
        _principal(),
        type="cron",
        target_kind="adopted_tool_run",
        input_text="scheduled",
        cron="* * * * *",
        instance_id=_INSTANCE,
        input_data={"channel": "email"},
    )
    assert row.target_kind == "adopted_tool_run"
    assert row.instance_id == _INSTANCE and row.input_data == {"channel": "email"}
    assert row.manifest_inline is None and row.manifest_ref is None
    assert "engine.schedule.register" in prov.events


async def test_register_adopted_tool_run_requires_instance_id() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError):  # no instance_id
        await svc.register(
            _principal(),
            type="cron",
            target_kind="adopted_tool_run",
            input_text="scheduled",
            cron="* * * * *",
        )


async def test_register_adopted_tool_run_forbids_a_manifest() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError):  # adopted_tool_run + a manifest is invalid
        await svc.register(
            _principal(),
            type="cron",
            target_kind="adopted_tool_run",
            input_text="scheduled",
            cron="* * * * *",
            instance_id=_INSTANCE,
            manifest_ref="h",
        )


async def test_register_harness_job_forbids_instance_id() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError):  # harness_job + instance_id is invalid
        await svc.register(
            _principal(),
            type="cron",
            target_kind="harness_job",
            input_text="go",
            cron="* * * * *",
            manifest_ref="h",
            instance_id=_INSTANCE,
        )


# ── fire branch: ADOPTED_TOOL_RUN (create-before-dispatch + no double-fire) (#489) ────────────────
async def test_fire_due_adopted_tool_creates_row_before_dispatch() -> None:
    sched = _adopted_schedule(cron="* * * * *", last_fired=None)
    srepo = _FakeSchedRepo([sched])
    jrepo = _FakeJobRepo()
    dispatches: list = []
    svc, prov = _svc(
        srepo,
        jrepo,
        enqueue_adopted_tool=lambda run, inst, data, o, u: dispatches.append((inst, data, o, u)),
    )
    fired = await svc.fire_due(_NOW)
    # the idempotency row was created (the dedupe gate) AND exactly one dispatch was enqueued
    assert fired == 1 and len(jrepo.adopted_created) == 1 and len(dispatches) == 1
    inst, data, org, user = dispatches[0]
    assert inst == _INSTANCE  # the curated instance is dispatched
    assert data == {"channel": "email", "content": "weekly digest"}  # the schedule's input_data
    assert org == _ORG and user == _USER  # the schedule-OWNER principal (no SYSTEM actor)
    assert srepo.rows[0].last_fired_at == _PREV  # cursor advanced
    assert "engine.schedule.fire" in prov.events
    # NO harness engine_job was created for an adopted-tool fire
    assert jrepo.created == []


async def test_fire_now_twice_same_window_dispatches_exactly_once() -> None:
    # THE merge gate: a duplicate same-window fire produces NO second registry dispatch. The
    # (org, idempotency_key) row is the gate — the second fire's create returns None, so the
    # enqueue callback is called EXACTLY ONCE across two same-window fires.
    sched = _adopted_schedule(cron="* * * * *", last_fired=None)
    srepo = _FakeSchedRepo([sched])
    jrepo = _FakeJobRepo()
    dispatches: list = []
    svc, _ = _svc(
        srepo,
        jrepo,
        enqueue_adopted_tool=lambda run, inst, data, o, u: dispatches.append(inst),
    )
    first = await svc.fire_now(sched.id, _principal())
    second = await svc.fire_now(sched.id, _principal())  # same window (now is fixed-ish by cursor)
    assert len(dispatches) == 1  # exactly one dispatch — the dedupe row blocked the second
    assert len(jrepo.adopted_created) == 1  # only one idempotency row
    assert first.last_fired_at is not None and second.last_fired_at == first.last_fired_at


async def test_create_adopted_tool_run_is_idempotent_on_org_key() -> None:
    # the repo-level dedupe: a second create on the same (org, key) returns None (the unique
    # constraint), so the fire branch never enqueues a second dispatch for that window.
    jrepo = _FakeJobRepo()
    sid = uuid.uuid4()
    key = f"{sid}:{_PREV.isoformat()}"
    first = await jrepo.create_adopted_tool_run(
        organisation_id=_ORG, schedule_id=sid, idempotency_key=key
    )
    second = await jrepo.create_adopted_tool_run(
        organisation_id=_ORG, schedule_id=sid, idempotency_key=key
    )
    assert first is not None and second is None
    assert jrepo.adopted_created == [key]  # only one row written


async def test_fire_now_adopted_tool_without_callback_is_a_hollow_noop() -> None:
    # the fire-now DI guard: if enqueue_adopted_tool is NOT injected, the branch creates the dedupe
    # row + advances the cursor but DISPATCHES NOTHING (a green-but-hollow path) — proving the DI
    # MUST inject the callback (it does, in get_schedule_service; guarded by the test below).
    sched = _adopted_schedule(cron="* * * * *", last_fired=None)
    srepo = _FakeSchedRepo([sched])
    jrepo = _FakeJobRepo()
    svc, prov = _svc(srepo, jrepo, enqueue_adopted_tool=None)  # callback NOT wired
    row = await svc.fire_now(sched.id, _principal())
    assert len(jrepo.adopted_created) == 1  # the dedupe row IS written...
    assert "engine.schedule.fire" not in prov.events  # ...but NOTHING was dispatched (hollow)
    assert row.last_fired_at is not None  # cursor still advances (window IS fired)


async def test_fire_now_adopted_tool_dispatches_when_callback_wired() -> None:
    # the positive of the DI guard: fire-now WITH the callback wired (as get_schedule_service does)
    # actually queues a dispatch — the path is not silently hollow.
    sched = _adopted_schedule(cron="* * * * *", last_fired=None)
    srepo = _FakeSchedRepo([sched])
    jrepo = _FakeJobRepo()
    dispatches: list = []
    svc, _ = _svc(
        srepo, jrepo, enqueue_adopted_tool=lambda run, inst, data, o, u: dispatches.append(inst)
    )
    await svc.fire_now(sched.id, _principal())
    assert dispatches == [_INSTANCE]  # fire-now queued exactly one registry dispatch


async def test_fire_now_missing_schedule_raises() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo(), enqueue_adopted_tool=lambda *a: None)
    with pytest.raises(ScheduleError):
        await svc.fire_now(uuid.uuid4(), _principal())


# ── #601: standing-team (target_kind="team") register + fire branch ───────────────────────────


async def test_register_team_valid() -> None:
    srepo, jrepo = _FakeSchedRepo(), _FakeJobRepo()
    svc, _ = _svc(srepo, jrepo)
    row = await svc.register(
        _principal(),
        type="cron",
        target_kind="team",
        manifest_inline={"members": []},
        input_text="standing team",
        cron="* * * * *",
        input_data={"sub_harnesses": {}, "gate_decisions": {}},
        graph_id="graph-1",
    )
    assert row.target_kind == "team" and row.graph_id == "graph-1" and row.instance_id is None


async def test_register_team_requires_graph_id() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError, match="graph_id"):
        await svc.register(
            _principal(),
            type="cron",
            target_kind="team",
            manifest_inline={"members": []},
            input_text="t",
            cron="* * * * *",
            graph_id=None,
        )


async def test_register_team_requires_inline_manifest() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError, match="inline team manifest"):
        await svc.register(
            _principal(),
            type="cron",
            target_kind="team",
            input_text="t",
            cron="* * * * *",
            graph_id="graph-1",
        )


async def test_register_team_forbids_instance_id() -> None:
    svc, _ = _svc(_FakeSchedRepo(), _FakeJobRepo())
    with pytest.raises(ScheduleError, match="instance_id"):
        await svc.register(
            _principal(),
            type="cron",
            target_kind="team",
            manifest_inline={"members": []},
            input_text="t",
            cron="* * * * *",
            instance_id=_INSTANCE,
            graph_id="graph-1",
        )


async def test_fire_due_team_run_creates_row_before_dispatch() -> None:
    srepo = _FakeSchedRepo([_team_schedule(cron="* * * * *")])
    jrepo, trepo = _FakeJobRepo(), _FakeTeamRunRepo()
    dispatches: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
    svc, prov = _svc(
        srepo,
        jrepo,
        enqueue_team_run=lambda run, o, u: dispatches.append((run, o, u)),
        team_runs=trepo,
    )
    fired = await svc.fire_due(_NOW)
    assert fired == 1 and len(trepo.team_created) == 1 and len(dispatches) == 1
    _run, org, user = dispatches[0]
    assert org == _ORG and user == _USER  # the schedule-OWNER principal (no SYSTEM actor)
    assert "engine.schedule.fire" in prov.events
    assert srepo.rows[0].last_fired_at == _PREV  # cursor advanced
    # the created run is bound to the schedule's persistent graph workspace (the keystone binding)
    assert trepo.team_rows[0].graph_id == "graph-1"


async def test_fire_team_run_without_callback_is_a_hollow_noop() -> None:
    # the DI guard: if enqueue_team_run is NOT injected, the team branch creates the dedupe row +
    # advances the cursor but NEVER dispatches (mirrors the adopted-tool hollow-noop guard).
    srepo = _FakeSchedRepo([_team_schedule(cron="* * * * *")])
    jrepo, trepo = _FakeJobRepo(), _FakeTeamRunRepo()
    svc, _ = _svc(srepo, jrepo, enqueue_team_run=None, team_runs=trepo)
    fired = await svc.fire_due(_NOW)
    assert fired == 0  # nothing dispatched
    assert len(trepo.team_created) == 1  # but the dedupe row WAS written
    assert srepo.rows[0].last_fired_at == _PREV  # and the cursor advanced


async def test_team_run_fire_is_idempotent_on_org_key() -> None:
    # the partial unique (org, idempotency_key=schedule:window): a duplicate same-window create
    # returns None → no second dispatch (create-before-enqueue dedupe).
    srepo = _FakeSchedRepo([_team_schedule(cron="* * * * *")])
    jrepo, trepo = _FakeJobRepo(), _FakeTeamRunRepo()
    dispatches: list[uuid.UUID] = []
    svc, _ = _svc(
        srepo, jrepo, enqueue_team_run=lambda r, o, u: dispatches.append(r), team_runs=trepo
    )
    await svc.fire_due(_NOW)
    srepo.rows[0].last_fired_at = None  # force a re-attempt of the SAME window → the create dedupes
    await svc.fire_due(_NOW)
    assert len(trepo.team_created) == 1 and len(dispatches) == 1  # exactly one run + one dispatch


async def test_list_team_runs_returns_the_schedules_runs_with_their_graph_binding() -> None:
    sched = _team_schedule(cron="* * * * *")
    srepo, jrepo, trepo = _FakeSchedRepo([sched]), _FakeJobRepo(), _FakeTeamRunRepo()
    svc, _ = _svc(srepo, jrepo, enqueue_team_run=lambda *a: None, team_runs=trepo)
    await svc.fire_due(_NOW)
    runs = await svc.list_team_runs(sched.id, _principal())
    assert len(runs) == 1
    assert runs[0].schedule_id == sched.id and runs[0].graph_id == "graph-1"
    # org-scoped: another org sees nothing
    assert await svc.list_team_runs(sched.id, _principal(org=uuid.uuid4())) == []
