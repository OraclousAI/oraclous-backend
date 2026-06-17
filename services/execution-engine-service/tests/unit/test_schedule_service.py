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

    async def create_scheduled(self, *, idempotency_key: str, **_kw: object):  # noqa: ANN202
        if idempotency_key in self.seen:  # the (org, key) unique constraint
            return None
        self.seen.add(idempotency_key)
        self.created.append(idempotency_key)
        return SimpleNamespace(id=uuid.uuid4())


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
    srepo: _FakeSchedRepo, jrepo: _FakeJobRepo, enqueue=None
) -> tuple[ScheduleService, _FakeProv]:  # noqa: ANN001
    prov = _FakeProv()
    svc = ScheduleService(
        schedules=srepo,  # type: ignore[arg-type]
        jobs=jrepo,  # type: ignore[arg-type]
        provenance=prov,  # type: ignore[arg-type]
        enqueue=enqueue,
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
